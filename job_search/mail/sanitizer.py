"""Bounded normalization for untrusted email content.

Sanitization is intentionally not treated as a prompt-injection defense by itself.
The returned text is visibly framed as untrusted, and downstream model execution and
proposal validation remain isolated and deterministic.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser


MAX_SANITIZED_CHARS = 24_000
MAX_SUBJECT_CHARS = 512

_HORIZONTAL_SPACE = re.compile(r"[\t\f\v ]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_QUOTED_HISTORY = (
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*on .{1,240} wrote:\s*$", re.I),
    re.compile(r"^\s*from:\s+.+$", re.I),
)
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "div", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
        "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }
)
_DISCARDED_TAGS = frozenset(
    {"head", "iframe", "noscript", "object", "script", "style", "svg", "template"}
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.discard_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in _DISCARDED_TAGS:
            self.discard_depth += 1
        elif not self.discard_depth and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() in _DISCARDED_TAGS:
            self.discard_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _DISCARDED_TAGS:
            self.discard_depth = max(0, self.discard_depth - 1)
        elif not self.discard_depth and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.discard_depth:
            self.parts.append(data)


@dataclass(frozen=True)
class SanitizedMail:
    """Normalized mail plus exact ranges that may support evidence."""

    text: str
    subject: str
    body: str
    subject_range: tuple[int, int]
    body_range: tuple[int, int]
    truncated: bool
    removed_characters: int
    content_sha256: str

    def verifies_evidence(self, quote: str, start: int, end: int) -> bool:
        if not quote or start < 0 or end <= start or end > len(self.text):
            return False
        if self.text[start:end] != quote:
            return False
        return any(
            range_start <= start and end <= range_end
            for range_start, range_end in (self.subject_range, self.body_range)
        )


def _drop_unsafe_unicode(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.replace("\r\n", "\n").replace("\r", "\n"))
    parts = []
    for character in normalized:
        category = unicodedata.category(character)
        if character in {"\n", "\t"}:
            parts.append(character)
        elif category in {"Cf", "Cs", "Co", "Cn"} or category.startswith("C"):
            continue
        else:
            parts.append(character)
    return "".join(parts)


def _normalize_lines(value: str, *, drop_history: bool) -> str:
    value = _drop_unsafe_unicode(value)
    lines: list[str] = []
    for line in value.split("\n"):
        line = _HORIZONTAL_SPACE.sub(" ", line).strip()
        if drop_history and lines and any(pattern.fullmatch(line) for pattern in _QUOTED_HISTORY):
            break
        lines.append(line)
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _html_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def sanitize_mail(
    subject: str,
    body: str,
    *,
    body_kind: str = "text",
    max_chars: int = MAX_SANITIZED_CHARS,
    drop_quoted_history: bool = True,
) -> SanitizedMail:
    """Return bounded inert text; legacy callers may omit quoted history."""

    if not isinstance(subject, str) or not isinstance(body, str):
        raise TypeError("subject and body must be strings")
    if max_chars < 1_024:
        raise ValueError("max_chars must be at least 1024")
    raw_size = len(subject) + len(body)
    clean_subject = _normalize_lines(html.unescape(subject), drop_history=False)
    clean_subject = clean_subject[:MAX_SUBJECT_CHARS]
    if body_kind.casefold() in {"html", "text/html"}:
        body_text = _html_text(body)
    elif body_kind.casefold() in {"text", "plain", "text/plain"}:
        body_text = html.unescape(body)
    else:
        raise ValueError("body_kind must be text or html")
    clean_body = _normalize_lines(body_text, drop_history=drop_quoted_history)

    prefix = "BEGIN UNTRUSTED EMAIL\nSUBJECT\n"
    middle = "\nBODY\n"
    suffix = "\nEND UNTRUSTED EMAIL"
    fixed_size = len(prefix) + len(middle) + len(suffix) + len(clean_subject)
    body_limit = max(0, max_chars - fixed_size)
    full_subject = _normalize_lines(html.unescape(subject), drop_history=False)
    truncated = len(clean_body) > body_limit or len(clean_subject) < len(full_subject)
    clean_body = clean_body[:body_limit]
    text = prefix + clean_subject + middle + clean_body + suffix
    subject_start = len(prefix)
    body_start = subject_start + len(clean_subject) + len(middle)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return SanitizedMail(
        text=text,
        subject=clean_subject,
        body=clean_body,
        subject_range=(subject_start, subject_start + len(clean_subject)),
        body_range=(body_start, body_start + len(clean_body)),
        truncated=truncated,
        removed_characters=max(0, raw_size - len(clean_subject) - len(clean_body)),
        content_sha256=digest,
    )
