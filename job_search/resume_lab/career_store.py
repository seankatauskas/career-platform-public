"""Private, immutable revisions of the user's reusable career fact bank.

Drafts are never evidence for generation. Approval is a separate, user-authored
event, and neither edits nor retirement changes an earlier approved revision.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import (ResumeBoundaryError, ResumeConflictError, ResumeLabError,
    ResumeNotFoundError, canonical_json, content_sha256, validate_identifier)
from .store import connect, _new_id, _now


CAREER_SECTIONS = ("education", "experience", "projects", "skills")
ENTRY_FIELDS = {
    "education": ("institution", "degree", "location", "dates", "details"),
    "experience": ("company", "role", "location", "dates"),
    "projects": ("name", "context", "dates", "url"),
    "skills": ("category",),
}
IDENTITY_FIELDS = ("name", "contact_line", "email", "linkedin", "github")
MAX_PROFILE_BYTES = 2 * 1024 * 1024
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS career_profile_revisions (
    revision_id TEXT PRIMARY KEY,
    parent_revision_id TEXT REFERENCES career_profile_revisions(revision_id),
    content_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS career_profile_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    draft_revision_id TEXT REFERENCES career_profile_revisions(revision_id),
    approved_revision_id TEXT REFERENCES career_profile_revisions(revision_id)
);
INSERT OR IGNORE INTO career_profile_state(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS career_profile_approvals (
    revision_id TEXT PRIMARY KEY REFERENCES career_profile_revisions(revision_id),
    actor TEXT NOT NULL CHECK(actor='user'),
    approved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS career_profile_commands (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS career_profile_revisions_no_update
BEFORE UPDATE ON career_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'career revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS career_profile_revisions_no_delete
BEFORE DELETE ON career_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'career revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS career_profile_approvals_no_update
BEFORE UPDATE ON career_profile_approvals BEGIN
    SELECT RAISE(ABORT, 'career approvals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS career_profile_approvals_no_delete
BEFORE DELETE ON career_profile_approvals BEGIN
    SELECT RAISE(ABORT, 'career approvals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS career_profile_commands_no_update
BEFORE UPDATE ON career_profile_commands BEGIN
    SELECT RAISE(ABORT, 'career commands are immutable'); END;
CREATE TRIGGER IF NOT EXISTS career_profile_commands_no_delete
BEFORE DELETE ON career_profile_commands BEGIN
    SELECT RAISE(ABORT, 'career commands are immutable'); END;
"""


def _text(value: Any, field: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ResumeLabError(f"{field} must be bounded text")
    return value.strip()


def _object(value: Any, field: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ResumeLabError(f"{field} has an invalid shape")
    return value


def _retired(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ResumeLabError("retired must be a boolean")
    return value


def empty_career_content() -> dict[str, Any]:
    return {"identity": {key: "" for key in IDENTITY_FIELDS}, "summary": "",
            **{section: [] for section in CAREER_SECTIONS}}


def normalize_career_content(content: Mapping[str, Any],
                             previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assign stable IDs and preserve removed records as retired facts.

    Editors should round-trip IDs when changing wording. A renderer-shaped import
    without IDs only inherits an existing ID on an exact content match.
    """
    _object(content, "career content", {"identity", "summary", *CAREER_SECTIONS})
    if len(canonical_json(content).encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ResumeLabError("career profile is too large")
    previous = previous or empty_career_content()
    identity = _object(content.get("identity", {}), "identity", set(IDENTITY_FIELDS))
    result = empty_career_content()
    result["identity"] = {key: _text(identity.get(key, ""), "identity." + key, 500 if key == "name" else 1000)
                          for key in IDENTITY_FIELDS}
    result["summary"] = _text(content.get("summary", ""), "summary")
    old_owners: dict[str, str] = {}
    for section in CAREER_SECTIONS:
        for entry in previous.get(section, []):
            old_owners[entry["entry_id"]] = section
            for fact in entry.get("items" if section == "skills" else "bullets", []):
                old_owners[fact["fact_id"]] = entry["entry_id"]
    seen: set[str] = set()

    def claim_id(value: str, owner: str) -> str:
        validate_identifier(value, "career fact ID")
        if value in old_owners and old_owners[value] != owner:
            raise ResumeBoundaryError("a career fact cannot move to a different role or section")
        if value in seen:
            raise ResumeLabError("career entry and fact IDs must be globally unique")
        seen.add(value)
        return value

    for section in CAREER_SECTIONS:
        entries = content.get(section, [])
        if not isinstance(entries, list) or len(entries) > 100:
            raise ResumeLabError(f"{section} must contain at most 100 entries")
        prior = {item["entry_id"]: item for item in previous.get(section, [])}
        list_key = "items" if section == "skills" else "bullets"
        for raw in entries:
            allowed = {"entry_id", "retired", *ENTRY_FIELDS[section]}
            if section != "education":
                allowed.add(list_key)
            _object(raw, section + " entry", allowed)
            values = {key: _text(raw.get(key, ""), section + "." + key,
                                4000 if key == "details" else 1000 if key in {"degree", "context", "url"} else 500)
                      for key in ENTRY_FIELDS[section]}
            eid = raw.get("entry_id")
            if not eid:
                match = next((item for item in prior.values() if item["entry_id"] not in seen
                              and all(item.get(k, "") == v for k, v in values.items())), None)
                eid = match["entry_id"] if match else _new_id("entry_")
            eid = claim_id(eid, section)
            normalized = {"entry_id": eid, "retired": _retired(raw.get("retired", False)), **values}
            if section != "education":
                facts = raw.get(list_key, [])
                if not isinstance(facts, list) or len(facts) > 100:
                    raise ResumeLabError("career entries must contain at most 100 facts")
                old_facts = {f["fact_id"]: f for f in prior.get(eid, {}).get(list_key, [])}
                normalized[list_key] = []
                for raw_fact in facts:
                    if isinstance(raw_fact, str):
                        raw_fact = {"text": raw_fact}
                    _object(raw_fact, "career fact", {"fact_id", "text", "retired"})
                    value = _text(raw_fact.get("text"), "career fact text", 500 if section == "skills" else 4000)
                    if not value:
                        raise ResumeLabError("career fact text must not be empty")
                    fid = raw_fact.get("fact_id")
                    if not fid:
                        match = next((f for f in old_facts.values() if f["fact_id"] not in seen and f["text"] == value), None)
                        fid = match["fact_id"] if match else _new_id("fact_")
                    fid = claim_id(fid, eid)
                    normalized[list_key].append({"fact_id": fid, "text": value,
                        "retired": _retired(raw_fact.get("retired", False))})
                for fid, fact in old_facts.items():
                    if fid not in seen:
                        claim_id(fid, eid)
                        normalized[list_key].append({**fact, "retired": True})
                if len(normalized[list_key]) > 100:
                    raise ResumeLabError("career entries exceed 100 facts including retired records")
            result[section].append(normalized)
        for eid, entry in prior.items():
            if eid not in seen:
                claim_id(eid, section)
                retired = copy.deepcopy(entry)
                retired["retired"] = True
                for fact in retired.get(list_key, []):
                    claim_id(fact["fact_id"], eid)
                result[section].append(retired)
        if len(result[section]) > 100:
            raise ResumeLabError(f"{section} exceeds 100 entries including retired records")
    if len(seen) > 2000 or len(canonical_json(result).encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ResumeLabError("career fact bank exceeds its size limit")
    return result


def renderable_content(content: Mapping[str, Any]) -> dict[str, Any]:
    result = {"identity": copy.deepcopy(content["identity"]), "summary": content.get("summary", "")}
    for section in CAREER_SECTIONS:
        result[section] = []
        for entry in content.get(section, []):
            if entry.get("retired"):
                continue
            values = {key: entry.get(key, "") for key in ENTRY_FIELDS[section]}
            if section != "education":
                key = "items" if section == "skills" else "bullets"
                values[key] = [fact["text"] for fact in entry.get(key, []) if not fact.get("retired")]
            result[section].append(values)
    return result


def career_source_facts(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Addressable bank fields and facts, retaining parent associations and retirement."""
    result = []
    for section in CAREER_SECTIONS:
        for i, entry in enumerate(content.get(section, [])):
            for key in ENTRY_FIELDS[section]:
                if entry.get(key):
                    result.append({"fact_id": entry["entry_id"] + ":" + key,
                        "entry_id": entry["entry_id"], "section": section,
                        "path": f"/{section}/{i}/{key}", "text": entry[key],
                        "retired": bool(entry.get("retired")), "kind": "field"})
            key = "items" if section == "skills" else "bullets"
            for j, fact in enumerate(entry.get(key, [])):
                result.append({"fact_id": fact["fact_id"], "entry_id": entry["entry_id"],
                    "section": section, "path": f"/{section}/{i}/{key}/{j}/text", "text": fact["text"],
                    "retired": bool(entry.get("retired") or fact.get("retired")),
                    "kind": "skill" if section == "skills" else "bullet"})
    return result


class CareerStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser().absolute()
        with connect(self.db_path) as con:
            con.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)

    @staticmethod
    def _revision(con, revision_id: str) -> dict[str, Any]:
        row = con.execute("SELECT * FROM career_profile_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if row is None:
            raise ResumeNotFoundError("career revision was not found")
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        result["provenance"] = json.loads(result.pop("provenance_json"))
        return result

    @classmethod
    def _profile(cls, con) -> dict[str, Any]:
        result = dict(con.execute("SELECT draft_revision_id,approved_revision_id FROM career_profile_state WHERE singleton=1").fetchone())
        for key in ("draft", "approved"):
            rid = result[key + "_revision_id"]
            result[key] = cls._revision(con, rid) if rid else None
        return result

    def get_profile(self) -> dict[str, Any]:
        with connect(self.db_path) as con:
            con.execute("BEGIN")
            return self._profile(con)

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        validate_identifier(revision_id, "revision_id")
        with connect(self.db_path) as con:
            return self._revision(con, revision_id)

    def is_approved(self, revision_id: str) -> bool:
        validate_identifier(revision_id, "revision_id")
        with connect(self.db_path) as con:
            return con.execute("SELECT 1 FROM career_profile_approvals WHERE revision_id=?", (revision_id,)).fetchone() is not None

    @staticmethod
    def _replay(con, key: str | None, operation: str, fingerprint: str):
        if key is None:
            return None
        validate_identifier(key, "idempotency_key")
        row = con.execute("SELECT * FROM career_profile_commands WHERE idempotency_key=?", (key,)).fetchone()
        if row:
            if row["operation"] != operation or row["request_sha256"] != fingerprint:
                raise ResumeConflictError("career command key belongs to a different request")
            return json.loads(row["result_json"])
        return None

    def replay_draft_command(self, key: str | None, request: Mapping[str, Any]):
        with connect(self.db_path) as con:
            return self._replay(con, key, "save_draft", content_sha256(request))

    @staticmethod
    def _record(con, key, operation, fingerprint, result):
        if key is not None:
            con.execute("INSERT INTO career_profile_commands VALUES(?,?,?,?,?)",
                (key, operation, fingerprint, canonical_json(result), _now()))

    def save_draft(self, content: Mapping[str, Any], *, expected_revision_id: str | None = None,
                   provenance: Mapping[str, Any] | None = None, actor: str = "user",
                   idempotency_key: str | None = None,
                   command_request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        validate_identifier(actor, "actor")
        if expected_revision_id is not None:
            validate_identifier(expected_revision_id, "expected_revision_id")
        provenance = dict(provenance or {"kind": "user_edit"})
        if len(canonical_json(provenance).encode("utf-8")) > MAX_PROFILE_BYTES:
            raise ResumeLabError("career provenance is too large")
        request = command_request if command_request is not None else {
            "content": content, "expected_revision_id": expected_revision_id,
            "provenance": provenance, "actor": actor}
        fingerprint = content_sha256(request)
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            replay = self._replay(con, idempotency_key, "save_draft", fingerprint)
            if replay is not None:
                return replay
            state = self._profile(con)
            if state["draft_revision_id"] != expected_revision_id:
                raise ResumeConflictError("career profile changed; reload before saving")
            previous = state["draft"]["content"] if state["draft"] else None
            normalized = normalize_career_content(content, previous)
            rid = _new_id("career_rev_")
            con.execute("INSERT INTO career_profile_revisions VALUES(?,?,?,?,?,?,?)",
                (rid, expected_revision_id, canonical_json(normalized), canonical_json(provenance),
                 content_sha256(normalized), actor, _now()))
            con.execute("UPDATE career_profile_state SET draft_revision_id=? WHERE singleton=1", (rid,))
            result = self._revision(con, rid)
            self._record(con, idempotency_key, "save_draft", fingerprint, result)
            return result

    def approve_revision(self, revision_id: str, *, actor: str = "user",
                         idempotency_key: str | None = None) -> dict[str, Any]:
        validate_identifier(revision_id, "revision_id")
        if actor != "user":
            raise ResumeBoundaryError("only the user can attest a career profile")
        fingerprint = content_sha256({"revision_id": revision_id, "actor": actor})
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            replay = self._replay(con, idempotency_key, "approve_revision", fingerprint)
            if replay is not None:
                return replay
            state = self._profile(con)
            if state["draft_revision_id"] != revision_id:
                raise ResumeConflictError("only the current draft can be approved; reload the profile")
            revision = self._revision(con, revision_id)
            content = revision["content"]
            if not content["identity"].get("name"):
                raise ResumeLabError("add your name before approving the career profile")
            if not any(not e["retired"] for s in CAREER_SECTIONS for e in content[s]):
                raise ResumeLabError("add at least one career entry before approving")
            required = {"education": ("institution", "degree"), "experience": ("company", "role"),
                        "projects": ("name",), "skills": ("category",)}
            for section in CAREER_SECTIONS:
                for entry in content[section]:
                    if not entry["retired"] and any(not entry[k] for k in required[section]):
                        raise ResumeLabError(f"complete the required {section} headings before approving")
            con.execute("INSERT OR IGNORE INTO career_profile_approvals VALUES(?,?,?)", (revision_id, actor, _now()))
            con.execute("UPDATE career_profile_state SET approved_revision_id=? WHERE singleton=1", (revision_id,))
            result = self._profile(con)
            self._record(con, idempotency_key, "approve_revision", fingerprint, result)
            return result

    def export_profile(self) -> dict[str, Any]:
        with connect(self.db_path) as con:
            con.execute("BEGIN")
            profile = self._profile(con)
            revisions = [self._revision(con, row["revision_id"]) for row in
                         con.execute("SELECT revision_id FROM career_profile_revisions ORDER BY rowid").fetchall()]
            approvals = [dict(row) for row in con.execute("SELECT * FROM career_profile_approvals ORDER BY rowid")]
            return {"schema_version": 1, "profile": profile, "revisions": revisions, "approvals": approvals}
