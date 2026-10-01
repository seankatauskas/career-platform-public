"""Bounded resume import into reviewable, never automatically attested drafts."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from job_search.mail.attachments import (AttachmentDescriptor, DOCX_MIME,
    SandboxedAttachmentExtractor, validate_attachment_bytes)

from .career_store import (CAREER_SECTIONS, ENTRY_FIELDS, IDENTITY_FIELDS,
    CareerStore, empty_career_content, normalize_career_content, renderable_content)
from .contracts import (ResumeBoundaryError, ResumeConflictError, ResumeLabError,
    validate_identifier)
from .pdf import PypdfExtractor


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_SOURCE_CHARS = 256_000
_CURRENT = object()
EXTRACTION_SCHEMA = {
    "content": {
        "identity": {key: "exact visible source text, or empty string" for key in IDENTITY_FIELDS},
        "summary": "exact source text, or empty string; never generate a summary",
        "education": [{key: "exact source text, or empty string" for key in ENTRY_FIELDS["education"]}],
        "experience": [{**{key: "exact source text, or empty string" for key in ENTRY_FIELDS["experience"]}, "bullets": ["exact source text"]}],
        "projects": [{**{key: "exact source text, or empty string" for key in ENTRY_FIELDS["projects"]}, "bullets": ["exact source text"]}],
        "skills": [{"category": "exact source category", "items": ["exact source skill"]}],
    },
    "source_spans": [{"path": "JSON pointer to every nonempty content string; preserve the source employer/project association",
        "start": "integer, inclusive Unicode character offset in source_text",
        "end": "integer, exclusive Unicode character offset in source_text"}],
}


def _source_text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_SOURCE_CHARS or "\x00" in text:
        raise ResumeLabError("resume source must contain bounded, nonempty text")
    # Do not strip or normalize: model offsets refer to this exact string.
    return text


def _fold(value: str) -> str:
    return " ".join(value.split()).casefold()


def _leaf_strings(value: Any, path: str = "") -> dict[str, str]:
    if isinstance(value, str):
        return {path: value} if value else {}
    result = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            result.update(_leaf_strings(item, path + "/" + str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.update(_leaf_strings(item, path + "/" + str(index)))
    return result


def _validate_associations(spans: list[dict[str, Any]]) -> None:
    """Reject moving a source bullet or structural field across career entries."""
    entries: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        match = re.match(r"^/(education|experience|projects|skills)/(\d+)/", span["path"])
        if match:
            entries.setdefault(match.group(0).rstrip("/"), []).append(span)
    starts = sorted((min(s["start"] for s in rows if not re.search(r"/(bullets|items)/", s["path"])), path)
                    for path, rows in entries.items()
                    if any(not re.search(r"/(bullets|items)/", s["path"]) for s in rows))
    for index, (start, path) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else MAX_SOURCE_CHARS + 1
        if any(s["start"] < start or s["end"] > end for s in entries[path]):
            raise ResumeBoundaryError("extraction moved a source field across an employer, project, or education entry")
    if set(entries) - {path for _, path in starts}:
        raise ResumeBoundaryError("extracted career facts require a source-backed entry heading")


def validate_extraction(output: Mapping[str, Any], text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Require source coverage for every imported value, independently of the model."""
    _source_text(text)
    if not isinstance(output, Mapping) or set(output) != {"content", "source_spans"}:
        raise ResumeBoundaryError("career extraction requires content and source_spans")
    raw = output["content"]
    if not isinstance(raw, Mapping):
        raise ResumeBoundaryError("extracted career content must be an object")
    # Source IDs and retirement are assigned by the application, never by extraction.
    for section in CAREER_SECTIONS:
        entries = raw.get(section, [])
        if not isinstance(entries, list):
            raise ResumeBoundaryError("extracted career sections must be arrays")
        for entry in entries:
            allowed = set(ENTRY_FIELDS[section]) | ({"items" if section == "skills" else "bullets"} if section != "education" else set())
            if not isinstance(entry, Mapping) or set(entry) - allowed:
                raise ResumeBoundaryError("extraction may not supply career record IDs or metadata")
            key = "items" if section == "skills" else "bullets"
            if key in entry and (not isinstance(entry[key], list) or any(not isinstance(f, str) for f in entry[key])):
                raise ResumeBoundaryError("extracted facts must contain only source text")
    normalized = normalize_career_content(raw)
    visible = renderable_content(normalized)
    leaves = _leaf_strings(visible)
    rows = output["source_spans"]
    if not isinstance(rows, list) or len(rows) > 2500:
        raise ResumeBoundaryError("career source spans must be a bounded array")
    paths = set()
    spans = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"path", "start", "end"}:
            raise ResumeBoundaryError("career source span has an invalid shape")
        path, start, end = row["path"], row["start"], row["end"]
        if not isinstance(path, str) or path not in leaves or path in paths:
            raise ResumeBoundaryError("career source span references an unknown or duplicate field")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end)) or not 0 <= start < end <= len(text):
            raise ResumeBoundaryError("career source span offset is invalid")
        # Whitespace wrapping is presentation, but names/casing/punctuation/numbers
        # are copied exactly at import. Rewording happens only after attestation.
        if " ".join(text[start:end].split()) != " ".join(leaves[path].split()):
            raise ResumeBoundaryError("an extracted career fact is not supported by its source span")
        paths.add(path)
        spans.append({"path": path, "start": start, "end": end, "text": text[start:end]})
    if paths != set(leaves):
        raise ResumeBoundaryError("every extracted career field requires a source span")
    if not leaves:
        raise ResumeLabError("resume extraction produced no career information")
    _validate_associations(spans)
    return normalized, spans


def _entry_key(section: str, entry: Mapping[str, Any]) -> tuple[str, ...]:
    keys = {"education": ("institution", "degree", "dates"), "experience": ("company", "role", "dates"),
            "projects": ("name", "dates"), "skills": ("category",)}[section]
    return tuple(_fold(str(entry.get(k, ""))) for k in keys)


def _merge_import(current: Mapping[str, Any] | None, incoming: Mapping[str, Any]):
    merged = copy.deepcopy(current or empty_career_content())
    review = []
    id_map: dict[str, str] = {}
    applied_paths: dict[str, bool] = {}
    for field in IDENTITY_FIELDS:
        value = incoming["identity"].get(field, "")
        previous = merged["identity"].get(field, "")
        applied_paths["/identity/" + field] = not previous or previous == value
        if value and previous and value != previous:
            review.append({"kind": "conflicting_field", "path": "/identity/" + field,
                "existing": previous, "incoming": value, "resolution": "existing_retained"})
        elif value:
            merged["identity"][field] = value
    value, previous = incoming.get("summary", ""), merged.get("summary", "")
    applied_paths["/summary"] = not previous or previous == value
    if value and previous and value != previous:
        review.append({"kind": "conflicting_field", "path": "/summary", "existing": previous,
            "incoming": value, "resolution": "existing_retained"})
    elif value:
        merged["summary"] = value
    for section in CAREER_SECTIONS:
        for i, incoming_entry in enumerate(incoming[section]):
            candidates = [e for e in merged[section] if _entry_key(section, e) == _entry_key(section, incoming_entry)]
            if len(candidates) > 1:
                # Ambiguity is visible rather than silently assigning accomplishments
                # to one of two positions with the same name and dates.
                review.append({"kind": "ambiguous_entry", "section": section,
                    "candidate_entry_ids": [e["entry_id"] for e in candidates], "resolution": "added_for_review"})
                candidates = []
            if not candidates:
                merged[section].append(copy.deepcopy(incoming_entry))
                id_map[incoming_entry["entry_id"]] = incoming_entry["entry_id"]
                for fact in incoming_entry.get("items" if section == "skills" else "bullets", []):
                    id_map[fact["fact_id"]] = fact["fact_id"]
                continue
            target = candidates[0]
            id_map[incoming_entry["entry_id"]] = target["entry_id"]
            review.append({"kind": "matching_entry", "section": section,
                "entry_id": target["entry_id"], "retired": target["retired"]})
            for field in ENTRY_FIELDS[section]:
                value, previous = incoming_entry[field], target[field]
                applied_paths[f"/{section}/{i}/{field}"] = not previous or previous == value
                if value and previous and value != previous:
                    review.append({"kind": "conflicting_field", "entry_id": target["entry_id"],
                        "field": field, "existing": previous, "incoming": value, "resolution": "existing_retained"})
                elif value:
                    target[field] = value
            key = "items" if section == "skills" else "bullets"
            for fact in incoming_entry.get(key, []):
                exact = next((f for f in target[key] if _fold(f["text"]) == _fold(fact["text"])), None)
                if exact:
                    id_map[fact["fact_id"]] = exact["fact_id"]
                    review.append({"kind": "duplicate_fact", "entry_id": target["entry_id"],
                        "fact_id": exact["fact_id"], "text": fact["text"], "resolution": "existing_retained"})
                else:
                    possible = [f for f in target[key]
                                if SequenceMatcher(None, _fold(f["text"]), _fold(fact["text"])).ratio() >= .82]
                    if possible:
                        review.append({"kind": "possible_duplicate", "entry_id": target["entry_id"],
                            "fact_id": fact["fact_id"], "candidate_fact_ids": [f["fact_id"] for f in possible],
                            "text": fact["text"], "resolution": "added_for_review"})
                    target[key].append(copy.deepcopy(fact))
                    id_map[fact["fact_id"]] = fact["fact_id"]
    return merged, review, id_map, applied_paths


def _provenance_spans(incoming, spans, id_map, applied):
    result = []
    for span in spans:
        value = {**span, "applied": applied.get(span["path"], True)}
        parts = span["path"].split("/")[1:]
        if parts[0] in CAREER_SECTIONS:
            entry = incoming[parts[0]][int(parts[1])]
            value["entry_id"] = id_map[entry["entry_id"]]
            if len(parts) == 4 and parts[2] in {"bullets", "items"}:
                value["fact_id"] = id_map[entry[parts[2]][int(parts[3])]["fact_id"]]
        result.append(value)
    return result


class CareerImporter:
    def __init__(self, store: CareerStore, *, extract_content: Callable[[str], Mapping[str, Any]] | None = None,
                 pdf_extractor: Any = None, attachment_extractor: Any = None) -> None:
        self.store = store
        self.extract_content = extract_content
        self.pdf_extractor = pdf_extractor
        self.attachment_extractor = attachment_extractor

    def _expected(self, expected_revision_id):
        if expected_revision_id is _CURRENT:
            return self.store.get_profile()["draft_revision_id"]
        return expected_revision_id

    def _import(self, text, *, expected_revision_id, idempotency_key, source,
                extracted=None, request=None):
        text = _source_text(text)
        request = request or {"kind": "career_import", "source": source,
            "expected_revision_id": "current" if expected_revision_id is _CURRENT else expected_revision_id,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
        replay = self.store.replay_draft_command(idempotency_key, request)
        if replay is not None:
            return replay
        expected_revision_id = self._expected(expected_revision_id)
        current = self.store.get_profile()
        if current["draft_revision_id"] != expected_revision_id:
            raise ResumeConflictError("career profile changed while import was queued; import again against the current draft")
        if extracted is None:
            if self.extract_content is None:
                raise ResumeLabError("configure career extraction or enter structured career information")
            extracted = self.extract_content(text)
        incoming, spans = validate_extraction(extracted, text)
        content, review, id_map, applied = _merge_import(current["draft"]["content"] if current["draft"] else None, incoming)
        # Surface meaningful text the extraction skipped so the review cannot look
        # complete merely because every returned field had a valid citation.
        cursor = 0
        for start, end in sorted((s["start"], s["end"]) for s in spans):
            if start > cursor and len(text[cursor:start].strip()) > 40:
                review.append({"kind": "unmapped_source", "start": cursor, "end": start,
                    "text": text[cursor:start].strip(), "resolution": "review_source"})
            cursor = max(cursor, end)
        if len(text[cursor:].strip()) > 40:
            review.append({"kind": "unmapped_source", "start": cursor, "end": len(text),
                "text": text[cursor:].strip(), "resolution": "review_source"})
        provenance = {"kind": "resume_import", "source": source, "source_text": text,
            "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_spans": _provenance_spans(incoming, spans, id_map, applied),
            "import_review": review, "attestation": "pending_user_review"}
        return self.store.save_draft(content, expected_revision_id=expected_revision_id,
            provenance=provenance, actor="import", idempotency_key=idempotency_key, command_request=request)

    def import_text(self, text: str, *, expected_revision_id=_CURRENT,
                    idempotency_key: str | None = None, source_name: str = "pasted resume") -> dict[str, Any]:
        source_name = str(source_name)
        if len(source_name) > 512 or "\x00" in source_name:
            raise ResumeLabError("resume source name is invalid")
        return self._import(text, expected_revision_id=expected_revision_id, idempotency_key=idempotency_key,
            source={"kind": "text", "name": source_name})

    def import_file(self, filename: str, data: bytes, *, expected_revision_id=_CURRENT,
                    idempotency_key: str | None = None) -> dict[str, Any]:
        if not isinstance(filename, str) or not filename or len(filename) > 512 or "\x00" in filename or PurePosixPath(filename.replace("\\", "/")).name != filename:
            raise ResumeLabError("resume filename must be a bounded name without a path")
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMPORT_BYTES:
            raise ResumeLabError("resume upload must contain at most 5 MiB")
        suffix = PurePosixPath(filename).suffix.casefold()
        if suffix not in {".pdf", ".docx", ".txt", ".md", ".tex", ".json"}:
            raise ResumeLabError("upload a PDF, DOCX, text, LaTeX resume, or exported career JSON")
        source = {"kind": suffix[1:], "name": filename, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        request = {"kind": "career_file_import", "source": copy.deepcopy(source),
            "expected_revision_id": "current" if expected_revision_id is _CURRENT else expected_revision_id}
        replay = self.store.replay_draft_command(idempotency_key, request)
        if replay is not None:
            return replay
        expected_revision_id = self._expected(expected_revision_id)
        if suffix == ".pdf":
            extraction = (self.pdf_extractor or PypdfExtractor()).extract(data)
            text = extraction.logical_text
            source["parser"] = extraction.parser
            source["parser_version"] = extraction.parser_version
        elif suffix == ".docx":
            descriptor = AttachmentDescriptor("career_" + source["sha256"][:24], filename, DOCX_MIME, len(data))
            validate_attachment_bytes(descriptor, data)
            extraction = (self.attachment_extractor or SandboxedAttachmentExtractor()).extract(descriptor, data)
            if extraction.truncated:
                raise ResumeLabError("resume text was truncated; import a smaller document")
            text = extraction.sanitized_text
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ResumeLabError("text and LaTeX resumes must be UTF-8") from exc
        if suffix == ".json":
            return self._import_json(text, source=source, expected_revision_id=expected_revision_id,
                                     idempotency_key=idempotency_key, request=request)
        return self._import(text, expected_revision_id=expected_revision_id, idempotency_key=idempotency_key,
                            source=source, request=request)

    def _import_json(self, text, *, source, expected_revision_id, idempotency_key, request):
        _source_text(text)
        try:
            value = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise ResumeLabError("career JSON is invalid") from exc
        if not isinstance(value, Mapping):
            raise ResumeLabError("career JSON must be an object")
        pointer = ""
        if set(value) == {"schema_version", "profile", "revisions", "approvals"} and value["schema_version"] == 1:
            profile = value["profile"]
            if not isinstance(profile, Mapping):
                raise ResumeLabError("exported career profile is invalid")
            key = "draft" if profile.get("draft") else "approved"
            revision = profile.get(key)
            if not isinstance(revision, Mapping) or not isinstance(revision.get("content"), Mapping):
                raise ResumeLabError("export has no career profile to import")
            raw = copy.deepcopy(revision["content"])
            pointer = "/profile/" + key + "/content"
        elif set(value) == {"content"}:
            raw = copy.deepcopy(value["content"])
            pointer = "/content"
        else:
            raw = copy.deepcopy(value)
        # A portable export carries historical IDs; local imports allocate their
        # own records and reconcile exact duplicates without accepting approval.
        if not isinstance(raw, Mapping):
            raise ResumeLabError("imported career content is invalid")
        original_content = copy.deepcopy(raw)
        for section in CAREER_SECTIONS:
            entries = raw.get(section, [])
            if not isinstance(entries, list):
                raise ResumeLabError("imported career sections must be arrays")
            for entry in entries:
                if isinstance(entry, dict):
                    entry.pop("entry_id", None)
                    facts = entry.get("items" if section == "skills" else "bullets", [])
                    if not isinstance(facts, list):
                        raise ResumeLabError("imported career facts must be arrays")
                    for fact in facts:
                        if isinstance(fact, dict):
                            fact.pop("fact_id", None)
        incoming = normalize_career_content(raw)
        current = self.store.get_profile()
        if current["draft_revision_id"] != expected_revision_id:
            raise ResumeConflictError("career profile changed while import was queued")
        content, review, id_map, applied = _merge_import(current["draft"]["content"] if current["draft"] else None, incoming)
        structured = {"identity": incoming["identity"], "summary": incoming["summary"]}
        for section in CAREER_SECTIONS:
            structured[section] = []
            for entry in incoming[section]:
                fields = {key: entry[key] for key in ENTRY_FIELDS[section]}
                if section != "education":
                    key = "items" if section == "skills" else "bullets"
                    fields[key] = [f["text"] for f in entry[key]]
                structured[section].append(fields)
        spans = []
        for path, val in _leaf_strings(structured).items():
            source_value = original_content
            for part in path.split("/")[1:]:
                source_value = source_value[int(part)] if isinstance(source_value, list) else source_value[part]
            source_pointer = pointer + path + ("/text" if isinstance(source_value, Mapping) else "")
            spans.append({"path": path, "source_pointer": source_pointer, "text": val})
        provenance = {"kind": "resume_import", "source": source, "source_text": text,
            "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_spans": _provenance_spans(incoming, spans, id_map, applied),
            "import_review": review, "attestation": "pending_user_review"}
        return self.store.save_draft(content, expected_revision_id=expected_revision_id,
            provenance=provenance, actor="import", idempotency_key=idempotency_key, command_request=request)

    def seed_standard(self, version: Mapping[str, Any], *, expected_revision_id=_CURRENT,
                      idempotency_key: str | None = None) -> dict[str, Any]:
        if not isinstance(version, Mapping) or not version.get("version_id") or not version.get("standard_id"):
            raise ResumeBoundaryError("career seeding requires an imported standard resume version")
        validate_identifier(version["version_id"], "standard_version_id")
        if version.get("purpose", "real_application") != "real_application" or any(
                c.get("origin") in {"synthetic_generated", "generated_wording"} for c in version.get("claims", [])):
            raise ResumeBoundaryError("generated resume artifacts cannot become source career facts")
        text = _source_text(version.get("plain_text"))
        source = {"kind": "standard", "standard_id": version["standard_id"], "version_id": version["version_id"]}
        request = {"kind": "career_standard_import", "source": copy.deepcopy(source),
            "expected_revision_id": "current" if expected_revision_id is _CURRENT else expected_revision_id}
        replay = self.store.replay_draft_command(idempotency_key, request)
        if replay is not None:
            return replay
        expected_revision_id = self._expected(expected_revision_id)
        metadata = version.get("import_metadata") or {}
        content = version.get("normalized_content")
        extracted = None
        if content is not None and metadata.get("normalization_claims") is not None:
            content = copy.deepcopy(content)
            for key in ("email", "linkedin", "github"):
                if isinstance(content.get("identity", {}).get(key), Mapping):
                    content["identity"][key] = content["identity"][key].get("display", "")
            for entry in content.get("projects", []):
                entry.pop("url", None)  # Imported PDF text does not attest hidden links.
            rows = [*metadata.get("fixed_fields", []), *metadata.get("normalization_claims", [])]
            spans = []
            for row in rows:
                path = row["path"]
                if re.fullmatch(r"/identity/(email|linkedin|github)/display", path):
                    path = path.rsplit("/", 1)[0]
                spans.append({"path": path, "start": row["source_start"], "end": row["source_end"]})
            extracted = {"content": content, "source_spans": spans}
            source["source_claim_ids"] = {row["path"]: row["claim_id"] for row in metadata.get("normalization_claims", [])}
        return self._import(text, expected_revision_id=expected_revision_id,
            idempotency_key=idempotency_key, source=source, extracted=extracted, request=request)
