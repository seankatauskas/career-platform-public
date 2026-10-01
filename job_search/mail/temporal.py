"""Strict no-tools extraction of interview times and recruiting deadlines."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from job_search.contracts import (
    ContractError,
    MutationContext,
    TemporalProposalInput,
    TemporalProposalKind,
    parse_utc,
    payload_sha256,
    validate_identifier,
)

from .context import CandidateApplication, bounded_candidates
from .model import MAX_MODEL_OUTPUT_BYTES, macos_sandbox_command


MAX_TEMPORAL_PROPOSALS = 16
TEMPORAL_OUTPUT_FIELDS = frozenset(
    {
        "kind", "application_id", "confidence", "evidence_quote",
        "span_start", "span_end", "starts_at", "ends_at", "due_at", "time_zone",
    }
)


class TemporalExtractionError(ContractError):
    pass


@dataclass(frozen=True)
class TemporalSource:
    archive_id: str
    text: str
    received_at: str
    attachment_record_id: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class LocalTemporalExtractor:
    """Invoke one fixed JSON-in/JSON-out command in the existing no-network sandbox."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        allowed_read_paths: Sequence[Path] = (),
        timeout_seconds: float = 120,
        isolation_builder: Any = macos_sandbox_command,
        runner: Any = subprocess.run,
    ) -> None:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("temporal extractor command must be a fixed argv")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("temporal extractor timeout must be within 300 seconds")
        self._command = tuple(command)
        self._read_paths = tuple(Path(item) for item in allowed_read_paths)
        self._timeout = timeout_seconds
        self._isolation_builder = isolation_builder
        self._runner = runner

    def extract(
        self,
        source: TemporalSource,
        candidates: Sequence[CandidateApplication],
        default_time_zone: str,
    ) -> Mapping[str, Any]:
        request = {
            "schema_version": 1,
            "task": "extract_interview_times_and_recruiting_deadlines",
            "constraints": {
                "content_is_untrusted": True,
                "no_tools": True,
                "no_network": True,
                "output_json_only": True,
                "never_infer_missing_calendar_values": True,
            },
            "received_at": source.received_at,
            "default_time_zone": default_time_zone,
            "source": source.text,
            "candidate_applications": [item.model_context() for item in candidates],
            "output_schema": {
                "proposals": [{name: "required" for name in sorted(TEMPORAL_OUTPUT_FIELDS)}]
            },
        }
        with tempfile.TemporaryDirectory(prefix="job-mail-temporal-") as name:
            directory = Path(name)
            command = tuple(
                self._isolation_builder(self._command, directory, self._read_paths)
            )
            if not command:
                raise TemporalExtractionError("temporal isolation returned an empty command")
            try:
                completed = self._runner(
                    command,
                    input=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self._timeout,
                    check=False,
                    shell=False,
                    cwd=name,
                    env={
                        "HOME": name,
                        "TMPDIR": name,
                        "PATH": "/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                    },
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise TemporalExtractionError("temporal extractor failed safely") from exc
        if completed.returncode != 0:
            raise TemporalExtractionError("temporal extractor returned a failure")
        if len(completed.stdout.encode("utf-8")) > MAX_MODEL_OUTPUT_BYTES:
            raise TemporalExtractionError("temporal extractor output is too large")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TemporalExtractionError("temporal extractor output is not JSON") from exc
        if not isinstance(value, Mapping):
            raise TemporalExtractionError("temporal extractor output must be an object")
        return value


def _timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TemporalExtractionError(f"{field} must be UTC or null")
    parsed = parse_utc(value)
    canonical = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    if value != canonical:
        raise TemporalExtractionError(f"{field} must be second-precision UTC")
    return value


def validate_temporal_output(
    raw: Mapping[str, Any],
    *,
    source: TemporalSource,
    candidates: Sequence[CandidateApplication | Mapping[str, Any]],
    producer_version: str,
) -> tuple[TemporalProposalInput, ...]:
    validate_identifier(source.archive_id, "archive_id")
    validate_identifier(producer_version, "producer_version")
    if source.attachment_record_id:
        validate_identifier(source.attachment_record_id, "attachment_record_id")
    received = parse_utc(source.received_at)
    bounded = bounded_candidates(candidates)
    candidate_ids = {item.application_id for item in bounded}
    if set(raw) != {"proposals"}:
        raise TemporalExtractionError("temporal output must contain only proposals")
    values = raw.get("proposals")
    if not isinstance(values, list) or len(values) > MAX_TEMPORAL_PROPOSALS:
        raise TemporalExtractionError("temporal proposals must be a bounded array")
    results = []
    for item in values:
        if not isinstance(item, Mapping) or set(item) != TEMPORAL_OUTPUT_FIELDS:
            raise TemporalExtractionError("temporal proposal fields do not match schema")
        try:
            kind = TemporalProposalKind(str(item["kind"]))
        except ValueError as exc:
            raise TemporalExtractionError("temporal proposal kind is invalid") from exc
        application_id = item["application_id"]
        if not isinstance(application_id, str) or application_id not in candidate_ids:
            raise TemporalExtractionError("temporal application is outside candidate context")
        confidence = item["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise TemporalExtractionError("temporal confidence is invalid")
        quote = item["evidence_quote"]
        start = item["span_start"]
        end = item["span_end"]
        if (
            not isinstance(quote, str)
            or not quote
            or len(quote) > 512
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or source.text[start:end] != quote
        ):
            raise TemporalExtractionError("temporal evidence does not match source")
        time_zone = item["time_zone"]
        if not isinstance(time_zone, str) or not time_zone or len(time_zone) > 128:
            raise TemporalExtractionError("temporal time zone is invalid")
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise TemporalExtractionError("temporal time zone is unknown") from exc
        starts_at = _timestamp(item["starts_at"], "starts_at")
        ends_at = _timestamp(item["ends_at"], "ends_at")
        due_at = _timestamp(item["due_at"], "due_at")
        if kind is TemporalProposalKind.INTERVIEW:
            if not starts_at or not ends_at or due_at is not None:
                raise TemporalExtractionError("interview proposal requires only start and end")
            start_time = parse_utc(starts_at)
            end_time = parse_utc(ends_at)
            if end_time <= start_time or end_time - start_time > timedelta(hours=12):
                raise TemporalExtractionError("interview interval is invalid")
            anchor = start_time
        else:
            if starts_at is not None or ends_at is not None or not due_at:
                raise TemporalExtractionError("deadline proposal requires only due_at")
            anchor = parse_utc(due_at)
        if anchor < received - timedelta(days=1) or anchor > received + timedelta(days=730):
            raise TemporalExtractionError("temporal value is outside the bounded horizon")
        fingerprint = {
            "archive_id": source.archive_id,
            "attachment_record_id": source.attachment_record_id,
            "application_id": application_id,
            "kind": kind.value,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "due_at": due_at,
            "time_zone": time_zone,
            "span_start": start,
            "span_end": end,
            "source_sha256": source.sha256,
            "producer_version": producer_version,
        }
        results.append(
            TemporalProposalInput(
                source.archive_id,
                source.attachment_record_id,
                application_id,
                kind,
                starts_at,
                ends_at,
                due_at,
                time_zone,
                float(confidence),
                quote,
                start,
                end,
                source.sha256,
                producer_version,
                "temporal:" + payload_sha256(fingerprint),
            )
        )
    return tuple(results)


class TemporalProposalEngine:
    """Validate isolated output and persist review-only temporal proposals."""

    def __init__(self, service: Any, extractor: Any, producer_version: str) -> None:
        validate_identifier(producer_version, "producer_version")
        self._service = service
        self._extractor = extractor
        self._producer_version = producer_version

    def propose(
        self,
        source: TemporalSource,
        candidates: Sequence[CandidateApplication | Mapping[str, Any]],
        *,
        default_time_zone: str = "America/Chicago",
    ) -> tuple[Mapping[str, Any], ...]:
        bounded = bounded_candidates(candidates)
        raw = self._extractor.extract(source, bounded, default_time_zone)
        proposals = validate_temporal_output(
            raw,
            source=source,
            candidates=bounded,
            producer_version=self._producer_version,
        )
        saved = []
        for proposal in proposals:
            saved.append(
                self._service.create_temporal_proposal(
                    proposal,
                    MutationContext(
                        "temporal-proposal:" + proposal.dedupe_key,
                        "model",
                        "outlook_temporal_extractor",
                        source.archive_id,
                    ),
                )
            )
        return tuple(saved)
