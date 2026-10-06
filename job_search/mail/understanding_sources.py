"""Prepare immutable, bounded sources before a shared analysis is claimed."""

from __future__ import annotations

import hashlib
import html
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import Any

from job_search.contracts import ContractError, payload_sha256

from .context import CandidateApplication, bounded_candidates
from .sanitizer import _BLOCK_TAGS, _DISCARDED_TAGS, _QUOTED_HISTORY, _normalize_lines
from .understanding_contracts import MAX_SOURCE_CHARS, SCHEMA_VERSION, validate_request

MAX_CURRENT_CHARS = 24_000
MAX_QUOTED_CHARS = 4_096
MAX_PRIOR_MESSAGES = 6
MAX_PRIOR_CHARS = 2_048
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_CHARS = 8_000


class _Segments(HTMLParser):
    """Retain quotation boundaries that disappear during ordinary HTML stripping."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[tuple[bool, str]] = []
        self.stack: list[tuple[str, bool, bool]] = []
        self.quoted = 0
        self.discarded = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attributes = dict(attrs)
        quote = tag == "blockquote" or "gmail_quote" in (attributes.get("class") or "").split() or (attributes.get("id") or "").casefold() == "divrplyfwdmsg"
        discard = tag in _DISCARDED_TAGS
        if tag not in {"br", "hr", "img", "meta", "link", "input", "wbr"}:
            self.stack.append((tag, quote, discard))
            self.quoted += int(quote)
            self.discarded += int(discard)
        if not self.discarded and tag in _BLOCK_TAGS:
            self.parts.append((bool(self.quoted), "\n"))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if not self.discarded and tag in _BLOCK_TAGS:
            self.parts.append((bool(self.quoted), "\n"))
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                removed = self.stack[index:]
                del self.stack[index:]
                self.quoted -= sum(item[1] for item in removed)
                self.discarded -= sum(item[2] for item in removed)
                break

    def handle_data(self, data):
        if not self.discarded:
            self.parts.append((bool(self.quoted), data))


def split_authored_text(subject: str, body: str, body_kind: str = "text") -> tuple[str, str]:
    """Sanitize without losing quoted history or treating it as a new request."""
    if not isinstance(subject, str) or not isinstance(body, str):
        raise ContractError("mail subject and body must be strings")
    if body_kind.casefold() in {"html", "text/html"}:
        parser = _Segments()
        parser.feed(body)
        parser.close()
        current = "".join(value for quoted, value in parser.parts if not quoted)
        quoted = "".join(value for is_quoted, value in parser.parts if is_quoted)
    elif body_kind.casefold() in {"text", "plain", "text/plain"}:
        current, quoted = html.unescape(body), ""
    else:
        raise ContractError("body_kind must be text or html")
    current = _normalize_lines(current, drop_history=False)
    quoted = _normalize_lines(quoted, drop_history=False)
    authored = []
    old = []
    history = False
    for line in current.split("\n"):
        if any(pattern.fullmatch(line) for pattern in _QUOTED_HISTORY):
            history = True
        if history or line.startswith(">"):
            old.append(line)
        else:
            authored.append(line)
    clean_subject = _normalize_lines(html.unescape(subject), drop_history=False)
    return (clean_subject + "\n\n" + "\n".join(authored)).strip(), ("\n".join(old) + "\n" + quoted).strip()


def build_request(
    *, account_id: str, immutable_message_id: str, observation_id: str,
    evidence_id: str, received_at: str, subject: str, body: str,
    body_kind: str = "text", candidates: Sequence[CandidateApplication | Mapping[str, Any]],
    candidate_context_complete: bool, producer_version: str,
    prior_messages: Sequence[Mapping[str, Any]] = (),
    attachments: Sequence[Mapping[str, Any]] = (),
    coverage: Sequence[Mapping[str, str]] = (), archive_id: str | None = None,
    replay_id: str | None = None,
) -> dict[str, Any]:
    current, quoted = split_authored_text(subject, body, body_kind)
    if not current:
        raise ContractError("shared mail understanding requires current message text")
    request: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "account_id": account_id,
        "immutable_message_id": immutable_message_id, "observation_id": observation_id,
        "evidence_id": evidence_id, "received_at": received_at,
        "sources": [], "candidates": [item.model_context() for item in bounded_candidates(candidates)],
        "candidate_context_complete": candidate_context_complete,
        "coverage": [dict(item) for item in coverage], "producer_version": producer_version,
    }
    if archive_id is not None:
        request["archive_id"] = archive_id
    if replay_id is not None:
        request["replay_id"] = replay_id

    def source_id(kind: str, suffix: str = "") -> str:
        return "mail-source:" + payload_sha256({"account_id": account_id, "message_id": immutable_message_id, "kind": kind, "suffix": suffix})

    def add(source: Mapping[str, Any], limit: int) -> None:
        value = dict(source)
        identity = value["source_id"]
        remaining = MAX_SOURCE_CHARS - sum(len(item["text"]) for item in request["sources"])
        supplied = value["text"][:min(limit, remaining)]
        if not supplied.strip():
            request["coverage"].append({"source_id": identity, "reason": "source_omitted_by_limit"})
            return
        was_truncated = bool(value.get("truncated", False)) or len(supplied) < len(value["text"])
        value.update(text=supplied, sha256=hashlib.sha256(supplied.encode("utf-8")).hexdigest(), truncated=was_truncated)
        request["sources"].append(value)
        if was_truncated:
            request["coverage"].append({"source_id": identity, "reason": "source_truncated"})

    metadata = {"source_at": received_at, **({"archive_id": archive_id} if archive_id else {})}
    add({"source_id": source_id("current"), "kind": "current", "text": current, **metadata}, MAX_CURRENT_CHARS)
    if quoted:
        add({"source_id": source_id("quoted"), "kind": "quoted", "text": quoted, **metadata}, MAX_QUOTED_CHARS)
    # The caller supplies archived source mappings. Order deterministically by time
    # and source ID so source selection is part of a repeatable request fingerprint.
    prior = sorted(prior_messages, key=lambda item: (str(item.get("source_at", "")), str(item.get("source_id", ""))), reverse=True)
    for index, item in enumerate(prior):
        if item.get("kind") not in {"prior_inbound", "prior_outbound"}:
            raise ContractError("prior message source kind is invalid")
        if index < MAX_PRIOR_MESSAGES:
            add(item, MAX_PRIOR_CHARS)
        else:
            request["coverage"].append({"source_id": item["source_id"], "reason": "prior_message_limit"})
    for index, item in enumerate(sorted(attachments, key=lambda item: str(item.get("source_id", "")))):
        if item.get("kind") != "attachment":
            raise ContractError("attachment source kind is invalid")
        if index < MAX_ATTACHMENTS:
            add(item, MAX_ATTACHMENT_CHARS)
        else:
            request["coverage"].append({"source_id": item["source_id"], "reason": "attachment_source_limit"})
    request["coverage"] = list({(item["source_id"], item["reason"]): item for item in request["coverage"]}.values())
    return validate_request(request)
