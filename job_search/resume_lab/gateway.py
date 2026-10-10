"""Production orchestration for job-scoped resume comparison runs.

The domain service below this module owns immutable facts and safety boundaries.  This
adapter joins it to the private PDF repository, the bounded local model, and the
application work queue.  The dashboard and Hermes see only sanitized mappings from
this adapter; TeX, claims, model prompts, storage locators, and job descriptions never
cross that product-facing boundary.
"""

from __future__ import annotations

import hashlib
import json
import importlib.util
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from job_search.contracts import canonical_json as ledger_json
from job_search.db import connect as ledger_connect
from job_search.db import prepare_database
from job_search.resume_integration import ResumeArtifactContent
from job_search.scheduler import utc_stamp
from job_search.worker import TaskContext

from .career_gateway import CareerGatewayMixin
from .artifacts import ArtifactNamespace, ResumePdfArtifactRepository
from .contracts import (
    ArtifactInput,
    ClaimOrigin,
    EvidenceStatus,
    JobSnapshot,
    RequirementGraph,
    ResumeBoundaryError,
    ResumeClaim,
    ResumeConflictError,
    ResumeLabError,
    ResumeNotFoundError,
    ResumePurpose,
    NORMALIZATION_VALIDATOR_REVISION,
    SCORER_REVISION,
    StandardVersionInput,
    VariantKind,
    canonical_json,
    content_sha256,
    sha256_text,
    validate_identifier,
)
from .fidelity import evaluate_pdf_fidelity
from .career_ops import normalize_ats_tokens
from .grounding import (
    GROUNDING_EQUIVALENCE_REVISION,
    GROUNDING_VALIDATOR_REVISION,
    curated_source_claims,
)
from .model import (
    MODEL_SCHEMA_VERSION,
    validate_standard_normalization_output,
    validate_variant_output,
)
from .requirements import (
    RequirementGraphLimitError,
    contains_protected_attribute_label,
    extract_requirement_graph,
    requirement_graph_from_clauses,
)
from .scoring import score_ats_proxy
from .service import ResumeLabService
from .store import RUNPOD_RECONCILIATION_ERROR, requires_runpod_reconciliation
from .tex import render_resume_tex


RESUME_OPTIMIZE_TASK = "resume.optimize"
GATEWAY_REVISION = "resume-gateway-v1"
MAX_SEMANTIC_ADJUDICATIONS = 24
SYNTHETIC_NOTICE = "SYNTHETIC RESEARCH BENCHMARK - NOT FOR APPLICATION"
NOTICE_SCORER_REVISION = (
    "ats-proxy-v2-evidence-association-system-notice-mask-v1"
)


def _has_current_normalization(version: Mapping[str, Any]) -> bool:
    imported = version.get("import_metadata")
    return (
        isinstance(version.get("normalized_content"), Mapping)
        and isinstance(imported, Mapping)
        and imported.get("normalization_validator_revision")
        == NORMALIZATION_VALIDATOR_REVISION
    )


class ResumeSetupError(ResumeLabError):
    """Required local-only generation infrastructure is not configured."""

    http_status = 503


class _ApplicationStateConflict(ResumeConflictError):
    """The authoritative application no longer permits resume mutation."""


class _ApplicationLedgerUnavailable(ResumeConflictError):
    """The authoritative application ledger could not be read safely."""

    http_status = 503


class _LostLease(RuntimeError):
    pass


class _LeaseGuard:
    """Keep a model-lane work lease alive and make lease loss observable."""

    def __init__(self, heartbeat: Callable[[], bool], *, interval: float = 30.0) -> None:
        self._heartbeat = heartbeat
        self._interval = interval
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._beat_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._keepalive,
            name="resume-model-lease",
            daemon=True,
        )

    def _beat(self) -> bool:
        with self._beat_lock:
            try:
                owned = bool(self._heartbeat())
            except Exception:
                owned = False
        if not owned:
            self._lost.set()
        return owned

    def _keepalive(self) -> None:
        while not self._stop.wait(self._interval):
            if not self._beat():
                return

    def __enter__(self) -> "_LeaseGuard":
        if not self._beat():
            raise _LostLease("resume worker lease was lost")
        self._thread.start()
        return self

    def check(self, *, refresh: bool = False) -> None:
        if self._lost.is_set() or (refresh and not self._beat()):
            raise _LostLease("resume worker lease was lost")

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)


def _safe_error_code(exc: BaseException) -> str:
    """Return a pathless/stable error code suitable for persisted run state."""

    if type(exc).__name__ == "RunpodReconciliationRequired":
        job_id = str(getattr(exc, "job_id", "") or "")
        if job_id and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", job_id):
            return f"{RUNPOD_RECONCILIATION_ERROR}:{job_id}"
        return RUNPOD_RECONCILIATION_ERROR
    known = {
        "ResumeModelError": "local_model_failed",
        "ResumeNormalizationRequired": "needs_normalization",
        "TexToolchainError": "tex_build_failed",
        "PdfExtractionError": "pdf_parse_failed",
        "PdfFidelityError": "pdf_fidelity_failed",
        "ArtifactIntegrityError": "artifact_integrity_failed",
        "ArtifactSecurityError": "artifact_security_failed",
        "ArtifactNotFoundError": "artifact_missing",
        "ContractError": "contract_rejected",
        "ResumeBoundaryError": "resume_boundary_rejected",
        "ResumeConflictError": "resume_conflict",
        "CareerPageOverflow": "career_one_page_overflow",
        "ResumeSetupError": "career_setup_required",
        "CareerImportError": "career_import_rejected",
    }
    return known.get(type(exc).__name__, "resume_processing_failed")


def _raise_if_runpod_reconciliation(exc: BaseException) -> None:
    """Never let an optional model fallback hide possibly accepted remote work."""
    from ..inference.usage import UsageDeferred, InvocationReconciliationRequired
    if isinstance(exc, (UsageDeferred, InvocationReconciliationRequired)) or type(exc).__name__ == "RunpodReconciliationRequired":
        raise exc


def _score_mapping(evaluation: Any) -> Mapping[str, Any]:
    criteria = sorted(
        evaluation.criteria,
        key=lambda item: (-float(item.weight), str(item.requirement_id)),
    )[:100]
    return {
        "evaluation_id": evaluation.cache_key,
        "score": evaluation.parsed_fit,
        "parsed_fit": evaluation.parsed_fit,
        "requirement_evidence": evaluation.requirement_evidence,
        "search_visibility": evaluation.search_visibility,
        "screening_readiness": evaluation.screening_readiness,
        "eligibility_status": evaluation.eligibility_status.value,
        "scorer_revision": evaluation.scorer_revision,
        "requirement_graph_fingerprint": evaluation.requirement_graph_fingerprint,
        "criteria": [
            {
                "requirement_id": item.requirement_id,
                "priority": item.priority.value,
                "kind": item.kind.value,
                "source_text": str(item.source_text)[:1_000],
                "status": item.status.value,
                "matched_terms": list(item.matched_terms),
                "missing_term_groups": [
                    list(group) for group in item.missing_term_groups
                ],
                "weight": item.weight,
                "value": item.value,
                "confidence": item.confidence,
                "match_method": item.match_method,
                "evidence_count": len(item.evidence),
            }
            for item in criteria
        ],
    }


def _claims(value: Sequence[Mapping[str, Any]]) -> tuple[ResumeClaim, ...]:
    return tuple(
        ResumeClaim(
            claim_id=str(item["claim_id"]),
            text=str(item["text"]),
            origin=ClaimOrigin(str(item["origin"])),
            source_fact_ids=tuple(
                str(part) for part in item.get("source_fact_ids", ())
            ),
        )
        for item in value
    )


def _fidelity_scalar(report: Any) -> float:
    return round(min(report.logical.token_recall, report.layout.token_recall), 6)


def _resume_dispatch_root(run_id: str, idempotency_key: str) -> str:
    return (
        "resume-optimize:"
        + hashlib.sha256(f"{run_id}\0{idempotency_key}".encode("utf-8")).hexdigest()
    )


def _resume_dispatch_rows(
    connection: sqlite3.Connection, dispatch_root: str
) -> list[sqlite3.Row]:
    generation_prefix = f"{dispatch_root}:dispatch:"
    return list(
        connection.execute(
            "SELECT work_id,status,lane,task_kind,dedupe_key,payload_json,"
            "schedule_key,workflow_id,parent_work_id FROM work_items "
            "WHERE dedupe_key=? OR substr(dedupe_key,1,?)=?",
            (dispatch_root, len(generation_prefix), generation_prefix),
        )
    )


def _public_dispatch(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "work_id": row["work_id"],
        "status": row["status"],
        "lane": row["lane"],
        "task_kind": row["task_kind"],
    }


def _active_resume_dispatch(
    application_db: Path,
    run_id: str,
    idempotency_key: Optional[str] = None,
) -> Mapping[str, Any] | None:
    """Best-effort reconciliation after an enqueue callback has returned badly."""

    payload = ledger_json({"run_id": run_id})
    parameters: tuple[Any, ...]
    identity_filter = ""
    if idempotency_key is None:
        parameters = (RESUME_OPTIMIZE_TASK, payload)
    else:
        dispatch_root = _resume_dispatch_root(run_id, idempotency_key)
        generation_prefix = f"{dispatch_root}:dispatch:"
        identity_filter = " AND (dedupe_key=? OR substr(dedupe_key,1,?)=?)"
        parameters = (
            RESUME_OPTIMIZE_TASK,
            payload,
            dispatch_root,
            len(generation_prefix),
            generation_prefix,
        )
    try:
        with ledger_connect(Path(application_db)) as connection:
            rows = list(
                connection.execute(
                    "SELECT work_id,status,lane,task_kind FROM work_items "
                    "WHERE task_kind=? AND lane='model' "
                    "AND status IN ('queued','running') AND payload_json=? "
                    + identity_filter
                    + " "
                    "ORDER BY created_at DESC,work_id DESC",
                    parameters,
                )
            )
    except (OSError, sqlite3.Error):
        return None
    return _public_dispatch(rows[0]) if rows else None


def enqueue_resume_run(
    application_db: Path,
    run_id: str,
    idempotency_key: str,
    *,
    now: Optional[datetime] = None,
) -> Mapping[str, Any]:
    """Durably enqueue only an opaque run ID on the model lane."""

    validate_identifier(run_id, "run_id")
    validate_identifier(idempotency_key, "idempotency_key")
    current = now or datetime.now(timezone.utc)
    stamp = utc_stamp(current)
    prepare_database(Path(application_db), stamp)
    with ledger_connect(Path(application_db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _enqueue_resume_run_in(
            connection, run_id, idempotency_key, stamp=stamp
        )


def _enqueue_resume_run_in(
    connection: sqlite3.Connection,
    run_id: str,
    idempotency_key: str,
    *,
    stamp: str,
) -> Mapping[str, Any]:
    """Enqueue using an existing application-ledger writer transaction."""

    validate_identifier(run_id, "run_id")
    validate_identifier(idempotency_key, "idempotency_key")
    dispatch_root = _resume_dispatch_root(run_id, idempotency_key)
    generation_prefix = f"{dispatch_root}:dispatch:"
    payload = ledger_json({"run_id": run_id})
    rows = _resume_dispatch_rows(connection, dispatch_root)
    generations: list[int] = []
    active: list[sqlite3.Row] = []
    for existing in rows:
        if (
            existing["task_kind"] != RESUME_OPTIMIZE_TASK
            or existing["lane"] != "model"
            or existing["payload_json"] != payload
            or existing["schedule_key"] is not None
            or existing["workflow_id"] != ""
            or existing["parent_work_id"] is not None
        ):
            raise RuntimeError("resume work dispatch identity conflicts")
        key = str(existing["dedupe_key"])
        if key == dispatch_root:
            generation = 0
        else:
            suffix = key[len(generation_prefix) :]
            if re.fullmatch(r"[1-9][0-9]{0,8}", suffix) is None:
                raise RuntimeError("resume work dispatch history is invalid")
            generation = int(suffix)
        generations.append(generation)
        if existing["status"] in {"queued", "running"}:
            active.append(existing)
        elif existing["status"] not in {"succeeded", "dead", "cancelled"}:
            raise RuntimeError("resume work dispatch status is invalid")
    if len(active) > 1:
        raise RuntimeError("resume work has multiple active dispatches")
    if active:
        return _public_dispatch(active[0])

    generation = max(generations, default=-1) + 1
    dedupe = dispatch_root if generation == 0 else f"{generation_prefix}{generation}"
    work_id = "work_" + uuid.uuid5(uuid.NAMESPACE_URL, dedupe).hex
    connection.execute(
        "INSERT INTO work_items "
        "(work_id,schedule_key,task_kind,dedupe_key,payload_json,status,priority,"
        "due_at,attempts,max_attempts,created_at,lane,workflow_id,parent_work_id) "
        "VALUES (?,NULL,?,?,?,'queued',50,?,0,5,?,'model','',NULL)",
        (work_id, RESUME_OPTIMIZE_TASK, dedupe, payload, stamp, stamp),
    )
    row = connection.execute(
        "SELECT work_id,status,lane,task_kind FROM work_items WHERE dedupe_key=?",
        (dedupe,),
    ).fetchone()
    if row is None:  # pragma: no cover - SQLite INSERT/SELECT invariant
        raise RuntimeError("resume work enqueue failed")
    return _public_dispatch(row)


class ResumeLabProductionGateway(CareerGatewayMixin):
    """Compose resume scoring/generation without granting either UI or Hermes internals."""

    def __init__(
        self,
        service: ResumeLabService,
        artifacts: ResumePdfArtifactRepository,
        *,
        application_db: Optional[Path] = None,
        model: Any = None,
        toolchain: Any = None,
        enqueue: Optional[Callable[[str, str], Mapping[str, Any]]] = None,
        now: Optional[Callable[[], datetime]] = None,
        allow_unmanaged_remote_inference: bool = False,
        application_gateway: Any = None,
    ) -> None:
        self.service = service
        self.artifacts = artifacts
        self.application_gateway = application_gateway
        self.application_db = (
            Path(application_db) if application_db is not None else None
        )
        self.model = model
        self._allow_unmanaged_remote_inference = allow_unmanaged_remote_inference
        self.toolchain = toolchain
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._uses_application_queue = enqueue is None and self.application_db is not None
        if enqueue is not None:
            self._enqueue_callback = enqueue
        elif self.application_db is not None:
            self._enqueue_callback = lambda run_id, key: enqueue_resume_run(
                self.application_db, run_id, key, now=self.now()
            )
        else:
            self._enqueue_callback = None

    @staticmethod
    def _job(value: Mapping[str, Any]) -> JobSnapshot:
        if not isinstance(value, Mapping):
            raise ResumeLabError("job must be an object")
        return JobSnapshot(
            ats=str(value.get("ats") or "").strip().lower(),
            job_id=str(value.get("id") or value.get("job_id") or "").strip(),
            title=str(value.get("title") or "").strip(),
            description=str(value.get("description") or "").strip(),
            employer=str(value.get("company") or value.get("employer") or "").strip(),
        )

    def _require_generation_ready(self) -> None:
        if self.model is None:
            raise ResumeSetupError("local resume model is not configured")
        if self.toolchain is None:
            raise ResumeSetupError("resume PDF toolchain is not configured")
        if self._enqueue_callback is None:
            raise ResumeSetupError("resume model queue is not configured")

    def _uses_queued_runpod_model(self) -> bool:
        config = getattr(self.model, "config", None)
        return getattr(config, "provider", None) == "runpod_serverless_vllm"

    def _model_calls_allowed(self) -> bool:
        """Keep HTTP previews deterministic while remote work owns a queue scope.

        The explicit standalone import CLI may opt out. This decision is local to
        each call: a dashboard request must never replace the shared model while
        another thread is generating a resume.
        """
        if self.model is None:
            return False
        if not self._uses_queued_runpod_model() or self._allow_unmanaged_remote_inference:
            return True
        from ..inference.usage import current_scope

        scope = current_scope()
        return bool(scope is not None and self.application_db is not None
                    and scope.db_path.resolve() == self.application_db.resolve())

    def _model_provenance(self) -> Mapping[str, Any]:
        """Return pathless model/config identity suitable for immutable metadata."""

        provider_identity = None
        if self.model is None:
            identity: Mapping[str, Any] = {"implementation": "deterministic-only"}
            producer = "deterministic-only"
            config_version = None
            model_sha256 = None
        else:
            config = getattr(self.model, "config", None)
            if config is not None:
                producer = str(
                    getattr(config, "producer_version", "local-resume-model")
                )
                config_version = getattr(config, "version", None)
                model_sha256 = getattr(config, "model_sha256", None)
                provenance = getattr(self.model, "provenance", None)
                if callable(provenance):
                    raw_identity = provenance()
                    safe_fields = {
                        "provider",
                        "producer_version",
                        "config_version",
                        "endpoint_fingerprint",
                        "model_id",
                        "model_revision",
                        "worker_image_digest",
                        "context_size",
                        "max_tokens",
                        "temperature",
                        "top_p",
                    }
                    if not isinstance(raw_identity, Mapping) or not set(
                        raw_identity
                    ).issubset(safe_fields):
                        raise ResumeSetupError(
                            "resume model provenance contains unsafe fields"
                        )
                    identity = dict(raw_identity)
                    provider = str(identity.get("provider") or "remote")
                    provider_identity = dict(identity)
                else:
                    identity = {
                        "producer_version": producer,
                        "config_version": config_version,
                        "model_sha256": model_sha256,
                        "command": [
                            str(value)
                            for value in getattr(config, "command", ())
                        ],
                        "allowed_read_paths": [
                            str(value)
                            for value in getattr(config, "allowed_read_paths", ())
                        ],
                        "timeout_seconds": getattr(
                            config, "timeout_seconds", None
                        ),
                    }
            else:
                producer = str(
                    getattr(self.model, "producer_version", type(self.model).__name__)
                )
                config_version = None
                model_sha256 = None
                identity = {"implementation": producer}
        result = {
            "producer_version": producer,
            "protocol_schema_version": MODEL_SCHEMA_VERSION,
            "config_version": config_version,
            "model_sha256": model_sha256,
            "config_sha256": content_sha256(identity),
        }
        if provider_identity is not None:
            result["provider"] = provider
            result["provider_identity"] = provider_identity
        return result

    @staticmethod
    def _validate_preparing_application_row(
        row: Optional[sqlite3.Row], application_id: str, job: JobSnapshot
    ) -> Mapping[str, Any]:
        validate_identifier(application_id, "application_id")
        if row is None:
            raise _ApplicationStateConflict("application was not found")
        if (
            str(row["ats"]).lower() != job.ats.lower()
            or str(row["job_id"]) != job.job_id
        ):
            raise _ApplicationStateConflict(
                "resume job does not match the application"
            )
        if str(row["current_phase"]) != "preparing":
            raise _ApplicationStateConflict(
                "resume changes are allowed only while preparing"
            )
        return dict(row)

    def _require_preparing_application_in(
        self,
        connection: sqlite3.Connection,
        application_id: str,
        job: JobSnapshot,
    ) -> Mapping[str, Any]:
        if self.application_gateway is not None:
            return self._require_owner_preparing(application_id, job)
        row = connection.execute(
            "SELECT ats,job_id,current_phase,last_event_seq,projection_sha256 "
            "FROM applications WHERE application_id=?",
            (application_id,),
        ).fetchone()
        return self._validate_preparing_application_row(row, application_id, job)

    def _require_owner_preparing(self, application_id, job):
        from ..commands import DomainError
        from ..contracts import ConflictError
        try:
            return self.application_gateway.require_document_editable(application_id, job)
        except (DomainError, ConflictError) as exc:
            raise _ApplicationStateConflict(str(exc)) from exc

    @contextmanager
    def _application_preparing_connection(self, application_id, job):
        """Owner reservation precedes the operational queue transaction everywhere."""
        from ..commands import DomainError
        from ..contracts import ConflictError
        authority = self.application_gateway.document_edit_lock(application_id, job) if self.application_gateway is not None else nullcontext()
        try:
            with authority:
                with ledger_connect(self.application_db) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if self.application_gateway is None:
                        self._require_preparing_application_in(connection, application_id, job)
                    yield connection
        except (DomainError, ConflictError) as exc:
            raise _ApplicationStateConflict(str(exc)) from exc

    def _require_preparing_application(
        self, application_id: str, job: JobSnapshot
    ) -> Mapping[str, Any]:
        """Bind resume work to the authoritative, still-editable application."""

        validate_identifier(application_id, "application_id")
        if self.application_gateway is not None:
            return self._require_owner_preparing(application_id, job)
        if self.application_db is None or not self.application_db.is_file():
            raise _ApplicationLedgerUnavailable("application ledger is unavailable")
        try:
            with ledger_connect(self.application_db) as connection:
                return self._require_preparing_application_in(
                    connection, application_id, job
                )
        except (OSError, sqlite3.Error) as exc:
            raise _ApplicationLedgerUnavailable(
                "application ledger is unavailable"
            ) from exc

    def _application_authority(self, application_id: str) -> Mapping[str, Any]:
        """Expose authority from the active application owner, not a display stage."""

        validate_identifier(application_id, "application_id")
        if self.application_gateway is not None:
            app = self.application_gateway.get_application_timeline(application_id)["application"]
            return {"application_phase": app["current_phase"], "selection_editable": app["disposition"] == "open"}
        if self.application_db is None or not self.application_db.is_file():
            raise _ApplicationLedgerUnavailable("application ledger is unavailable")
        try:
            with ledger_connect(self.application_db) as connection:
                row = connection.execute(
                    "SELECT current_phase FROM applications WHERE application_id=?",
                    (application_id,),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise _ApplicationLedgerUnavailable(
                "application ledger is unavailable"
            ) from exc
        if row is None:
            raise ResumeNotFoundError("application was not found")
        phase = str(row["current_phase"])
        return {
            "application_phase": phase,
            "selection_editable": phase == "preparing",
        }

    def _while_application_preparing(
        self,
        application_id: str,
        job: JobSnapshot,
        operation: Callable[[sqlite3.Connection], Any],
    ) -> Any:
        """Serialize one sidecar commit with authoritative phase changes."""

        validate_identifier(application_id, "application_id")
        if self.application_db is None or not self.application_db.is_file():
            raise _ApplicationLedgerUnavailable("application ledger is unavailable")
        try:
            with self._application_preparing_connection(application_id, job) as connection:
                return operation(connection)
        except _ApplicationStateConflict:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise _ApplicationLedgerUnavailable(
                "application ledger is unavailable"
            ) from exc

    @staticmethod
    def _enqueue_key(run: Mapping[str, Any]) -> str:
        target_attempt = int(run["attempt"]) + 1
        return "enqueue_" + content_sha256(
            {
                "run_id": run["run_id"],
                "target_attempt": target_attempt,
            }
        )

    def _enqueue_run_while_locked(
        self,
        application_connection: sqlite3.Connection,
        run: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Dispatch a run before releasing the authoritative phase writer lock."""

        if run["status"] not in {"queued", "running"}:
            return None
        if not self._uses_application_queue:
            return self._enqueue_run(run, "phase_locked_dispatch")
        if run["status"] == "running":
            payload = ledger_json({"run_id": run["run_id"]})
            active = application_connection.execute(
                "SELECT work_id,status,lane,task_kind FROM work_items "
                "WHERE task_kind=? AND lane='model' "
                "AND status IN ('queued','running') AND payload_json=? "
                "ORDER BY created_at DESC,work_id DESC LIMIT 1",
                (RESUME_OPTIMIZE_TASK, payload),
            ).fetchone()
            if active is not None:
                return _public_dispatch(active)
        return _enqueue_resume_run_in(
            application_connection,
            str(run["run_id"]),
            self._enqueue_key(run),
            stamp=utc_stamp(self.now()),
        )

    def _enqueue_run(
        self, run: Mapping[str, Any], idempotency_key: str
    ) -> Mapping[str, Any] | None:
        validate_identifier(idempotency_key, "idempotency_key")
        if run["status"] not in {"queued", "running"}:
            return None
        assert self._enqueue_callback is not None
        enqueue_key = self._enqueue_key(run)
        if run["status"] == "running" and self.application_db is not None:
            active = _active_resume_dispatch(
                self.application_db, str(run["run_id"])
            )
            if active is not None:
                return active
        try:
            return self._enqueue_callback(str(run["run_id"]), enqueue_key)
        except Exception:
            if self.application_db is not None:
                reconciled = _active_resume_dispatch(
                    self.application_db,
                    str(run["run_id"]),
                    enqueue_key,
                )
                if reconciled is not None:
                    return reconciled
            if run["status"] == "queued":
                self.service.fail_queued_run(
                    str(run["run_id"]), error="queue_enqueue_failed"
                )
            raise

    @staticmethod
    def _model_clauses(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        rows = value.get("requirements")
        if not isinstance(rows, list):
            raise ResumeLabError("model requirements are invalid")
        return [
            {
                "text": item["text"],
                "source_start": item["source_start"],
                "source_end": item["source_end"],
                "kind": item["kind"],
            }
            for item in rows
        ]

    def _extract_graph(
        self, job: JobSnapshot
    ) -> tuple[RequirementGraph, list[Mapping[str, Any]], str]:
        if not self._model_calls_allowed():
            return extract_requirement_graph(job), [], "deterministic_fallback"
        try:
            clauses = self._model_clauses(
                self.model.extract_requirements(job.description)
            )
            graph = requirement_graph_from_clauses(job, clauses, include_fallback=True)
        except Exception as exc:
            _raise_if_runpod_reconciliation(exc)
            return extract_requirement_graph(job), [], "deterministic_fallback"
        return graph, clauses, "local_model_plus_fallback"

    @staticmethod
    def _semantic_candidates(
        resume_text: str, claims: Sequence[Mapping[str, Any]]
    ) -> tuple[list[Mapping[str, Any]], Mapping[str, tuple[str, int]]]:
        candidates = []
        locations: dict[str, tuple[str, int]] = {}
        cursor_by_text: dict[str, int] = {}
        for claim in claims:
            text = str(claim.get("text") or "")
            claim_id = str(claim.get("claim_id") or "")
            if contains_protected_attribute_label(text):
                continue
            start = resume_text.find(text, cursor_by_text.get(text, 0)) if text else -1
            if start < 0 and text:
                start = resume_text.find(text)
            if claim_id and text and start >= 0:
                candidates.append(
                    {"claim_id": claim_id, "text": text, "section": "resume"}
                )
                locations[claim_id] = (text, start)
                cursor_by_text[text] = start + len(text)
        bounded = candidates[:500]
        return bounded, {
            str(item["claim_id"]): locations[str(item["claim_id"])] for item in bounded
        }

    @staticmethod
    def _mask_system_notice(resume_text: str) -> tuple[str, bool]:
        pattern = re.compile(
            r"SYNTHETIC\s+RESEARCH\s+BENCHMARK\s*[-–—]\s*NOT\s+FOR\s+APPLICATION",
            re.IGNORECASE,
        )
        masked, count = pattern.subn(
            lambda match: " " * len(match.group(0)), resume_text
        )
        return masked, bool(count)

    def _score(
        self,
        resume_text: str,
        graph: RequirementGraph,
        claims: Sequence[Mapping[str, Any]],
    ) -> Any:
        scored_text, notice_masked = self._mask_system_notice(resume_text)
        revision = NOTICE_SCORER_REVISION if notice_masked else None
        lexical = score_ats_proxy(
            scored_text,
            graph,
            **({"scorer_revision": revision} if revision else {}),
        )
        if not self._model_calls_allowed():
            return self._restore_full_text_identity(lexical, resume_text)
        candidates, locations = self._semantic_candidates(scored_text, claims)
        if not candidates:
            return self._restore_full_text_identity(lexical, resume_text)
        requirements = {item.requirement_id: item for item in graph.requirements}
        unresolved = [
            item
            for item in lexical.criteria
            if item.status in {EvidenceStatus.UNKNOWN, EvidenceStatus.NOT_EVIDENCED}
            and item.weight > 0
        ]
        unresolved.sort(key=lambda item: (-item.weight, item.requirement_id))
        unresolved = unresolved[:MAX_SEMANTIC_ADJUDICATIONS]
        requests = []
        for criterion in unresolved:
            requirement = requirements[criterion.requirement_id]
            requests.append(
                {
                    "requirement_id": requirement.requirement_id,
                    "text": requirement.source_text,
                    "kind": requirement.kind.value,
                    "priority": requirement.priority.value,
                    "minimum_years": requirement.minimum_years,
                    "term_groups": [
                        list(group) for group in requirement.term_groups
                    ],
                }
            )
        overrides: dict[str, Mapping[str, Any]] = {}

        def record(requirement_id: str, result: Mapping[str, Any]) -> None:
            status = str(result["status"])
            if status not in {"met", "partial", "contradicted"}:
                return
            spans = []
            occupied: set[tuple[int, int]] = set()
            for evidence in result.get("evidence", ()):
                claim_id = str(evidence.get("claim_id") or "")
                quote = str(evidence.get("quote") or "")
                claim_location = locations.get(claim_id)
                if claim_location is None:
                    continue
                claim_text, claim_start = claim_location
                relative = claim_text.find(quote)
                start = claim_start + relative if relative >= 0 else -1
                if (
                    not quote
                    or start < 0
                    or (start, start + len(quote)) in occupied
                ):
                    continue
                occupied.add((start, start + len(quote)))
                spans.append(
                    {"text": quote, "start": start, "end": start + len(quote)}
                )
                if len(spans) == 3:
                    break
            if not spans:
                return
            overrides[requirement_id] = {
                "status": status,
                "confidence": result["confidence"],
                "evidence": spans,
            }

        batch = getattr(self.model, "adjudicate_evidence_batch", None)
        if requests and callable(batch):
            try:
                result = batch(requests, candidates)
                rows = result.get("adjudications", ())
                requested_ids = {
                    str(request["requirement_id"]) for request in requests
                }
                seen: set[str] = set()
                for row in rows:
                    requirement_id = str(row.get("requirement_id") or "")
                    if requirement_id not in requested_ids or requirement_id in seen:
                        raise ValueError("invalid batch evidence identity")
                    seen.add(requirement_id)
                    record(requirement_id, row)
                if seen != requested_ids:
                    raise ValueError("incomplete batch evidence")
            except Exception as exc:
                _raise_if_runpod_reconciliation(exc)
                # Semantic evidence is optional. A malformed batch cannot break or
                # partially influence deterministic scoring.
                overrides.clear()
        else:
            # Compatibility for a custom protocol-v1 model implementation. The
            # bundled production driver always takes the single batch path above.
            for request in requests:
                try:
                    result = self.model.adjudicate_evidence(request, candidates)
                    record(str(request["requirement_id"]), result)
                except Exception as exc:
                    _raise_if_runpod_reconciliation(exc)
                    continue
        evaluated = score_ats_proxy(
            scored_text,
            graph,
            semantic_overrides=overrides,
            **({"scorer_revision": revision} if revision else {}),
        )
        return self._restore_full_text_identity(evaluated, resume_text)

    @staticmethod
    def _restore_full_text_identity(evaluation: Any, resume_text: str) -> Any:
        full_hash = sha256_text(resume_text)
        if evaluation.artifact_text_sha256 == full_hash:
            return evaluation
        cache_key = content_sha256(
            {
                "scored_evaluation_cache_key": evaluation.cache_key,
                "full_artifact_text_sha256": full_hash,
                "system_notice_policy": NOTICE_SCORER_REVISION,
            }
        )
        return replace(
            evaluation,
            artifact_text_sha256=full_hash,
            cache_key=cache_key,
        )

    @staticmethod
    def _public_standard(
        standard: Mapping[str, Any], version: Optional[Mapping[str, Any]] = None
    ) -> Mapping[str, Any]:
        version = version or {}
        imported = version.get("import_metadata") or {}
        return {
            "standard_id": standard["standard_id"],
            "standard_version_id": standard["active_version_id"],
            "active_version_id": standard["active_version_id"],
            "name": standard["name"],
            "manual_rank": standard["manual_rank"],
            "status": "active" if standard.get("active", True) else "historical",
            "normalized": _has_current_normalization(version),
            "parse_safe": bool(imported.get("parse_safe")),
        }

    def list_standards(self, *, limit: int = 25) -> Mapping[str, Any]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 25
        ):
            raise ResumeLabError("standard limit must be between 1 and 25")
        result = []
        for standard in self.service.list_active_standards()[:limit]:
            version = self.service.store.get_standard_version(
                str(standard["active_version_id"])
            )
            public = dict(self._public_standard(standard, version))
            imported = version.get("import_metadata") or {}
            if imported.get("managed_relative_path") and imported.get("pdf_sha256"):
                document_url = f"/api/v1/resume-lab/standards/{standard['active_version_id']}/document"
                public.update(document_url=document_url, preview_url=f"{document_url}?disposition=inline")
            result.append(public)
        return {"standards": result, "gateway_revision": GATEWAY_REVISION}

    def _build_standard_version(
        self, tex_source: str
    ) -> tuple[StandardVersionInput, str, Optional[str]]:
        """Run the one import/update pipeline for user-authored TeX."""

        if self.toolchain is None:
            raise ResumeSetupError("resume PDF toolchain is not configured")
        compiled = self.toolchain.compiler.compile(tex_source)
        extracted = self.toolchain.extractor.extract(compiled.pdf_bytes)
        plain_text = extracted.logical_text.strip()
        fidelity = evaluate_pdf_fidelity(plain_text, extracted)
        if fidelity.status != "pass":
            raise ResumeBoundaryError(
                "hand-written resume failed the ATS PDF fidelity gate"
            )
        saved_pdf = self.artifacts.write_pdf(compiled.pdf_bytes, ArtifactNamespace.REAL)
        normalized_content: Optional[Mapping[str, Any]] = None
        normalized_claims: list[Mapping[str, Any]] = []
        fixed_fields: list[Mapping[str, Any]] = []
        normalization_status = "needs_normalization"
        model_allowed = self._model_calls_allowed()
        normalization_error = ("model_config_missing" if self.model is None else
                               "managed_model_work_required" if not model_allowed else "")
        if model_allowed:
            try:
                normalized = validate_standard_normalization_output(
                    self.model.normalize_standard_resume(tex_source, plain_text),
                    plain_text,
                )
                normalized_content = dict(normalized["content"])
                normalized_claims = [dict(item) for item in normalized["claims"]]
                fixed_fields = [dict(item) for item in normalized["fixed_fields"]]
                normalization_status = "ready"
            except Exception as exc:
                _raise_if_runpod_reconciliation(exc)
                normalization_error = _safe_error_code(exc)
        if normalized_claims:
            claims = tuple(
                ResumeClaim(
                    str(item["claim_id"]),
                    str(item["text"]),
                    ClaimOrigin.USER_ATTESTED,
                )
                for item in normalized_claims
            )
        else:
            lines = [line.strip() for line in plain_text.splitlines() if line.strip()]
            claims = tuple(
                ResumeClaim(
                    "fact_"
                    + hashlib.sha256(f"{index}\0{line}".encode()).hexdigest()[:24],
                    line,
                    ClaimOrigin.USER_ATTESTED,
                )
                for index, line in enumerate(lines[:500])
            )
        if not claims:
            raise ResumeBoundaryError("parsed resume contains no claimable text")
        import_metadata = {
            "managed_relative_path": saved_pdf.managed_relative_path,
            "pdf_sha256": saved_pdf.sha256,
            "parsed_text": plain_text,
            "parse_fidelity": _fidelity_scalar(fidelity),
            "parse_safe": True,
            "source_sha256": compiled.source_sha256,
            "compiler": compiled.engine,
            "compiler_version": compiled.engine_version,
            "bundle_sha256": compiled.bundle_sha256,
            "parser": extracted.parser,
            "parser_version": extracted.parser_version,
            "pages": extracted.pages,
            "content_stream_bytes": extracted.content_stream_bytes,
            "layout_text_sha256": sha256_text(extracted.layout_text),
            "fidelity": asdict(fidelity),
            "normalization_claims": normalized_claims,
            "fixed_fields": fixed_fields,
            "normalization_validator_revision": (
                NORMALIZATION_VALIDATOR_REVISION
                if normalization_status == "ready"
                else None
            ),
            "model_provenance": self._model_provenance(),
        }
        return (
            StandardVersionInput(
                tex_source=tex_source,
                plain_text=plain_text,
                claims=claims,
                normalized_content=normalized_content,
                import_metadata=import_metadata,
            ),
            normalization_status,
            normalization_error or None,
        )

    def import_existing_standard(self, name: str, manual_rank: int, *, pdf: bytes,
                                 tex_source: str, intended_text: str, content=None,
                                 provenance=None, extractor=None) -> Mapping[str, Any]:
        """Import a user-authored PDF unchanged; no compiler or model calls."""
        from .pdf import PypdfExtractor, MAX_PDF_BYTES
        if len(pdf) > MAX_PDF_BYTES or not pdf.startswith(b"%PDF-"):
            raise ResumeBoundaryError("invalid or oversized resume PDF")
        if not tex_source.strip() or len(tex_source.encode()) > 1_000_000:
            raise ResumeBoundaryError("matching LaTeX source is required")
        extracted = (extractor or (self.toolchain.extractor if self.toolchain else PypdfExtractor())).extract(pdf)
        fidelity = evaluate_pdf_fidelity(intended_text, extracted)
        if fidelity.status != "pass" or extracted.pdf_sha256 != hashlib.sha256(pdf).hexdigest():
            raise ResumeBoundaryError("resume PDF does not match the supplied text")
        digest = hashlib.sha256(pdf).hexdigest()
        # A repeat import returns the existing immutable version, not a duplicate.
        for standard in self.service.list_active_standards():
            version = self.service.store.get_standard_version(str(standard['active_version_id']))
            metadata = version.get('import_metadata') or {}
            if standard['name'] == name and metadata.get('pdf_sha256') == digest and version['tex_source'] == tex_source:
                return self._public_standard(standard, version)
        saved = self.artifacts.write_pdf(pdf, ArtifactNamespace.REAL)
        text = extracted.logical_text.strip()
        claims = tuple(ResumeClaim('fact_' + hashlib.sha256(f'{i}\0{line}'.encode()).hexdigest()[:24],
                                  line, ClaimOrigin.USER_ATTESTED)
                       for i,line in enumerate(text.splitlines()) if line.strip())[:500]
        if not claims: raise ResumeBoundaryError('PDF contains no readable resume text')
        metadata = {'managed_relative_path':saved.managed_relative_path,'pdf_sha256':saved.sha256,
                    'parsed_text':text,'parse_safe':True,'parse_fidelity':_fidelity_scalar(fidelity),
                    'source_sha256':sha256_text(tex_source),'parser':extracted.parser,
                    'parser_version':extracted.parser_version,'pages':extracted.pages,
                    'content_stream_bytes':extracted.content_stream_bytes,'fidelity':asdict(fidelity),
                    'import_kind':'existing_pdf','source_provenance':dict(provenance or {}),
                    'normalization_validator_revision':None}
        result=self.service.create_standard(name,manual_rank,StandardVersionInput(
            tex_source=tex_source,plain_text=text,claims=claims,normalized_content=content,
            import_metadata=metadata),actor_kind='user')
        return self._public_standard(result['standard'],result['version'])

    def use_standard(self, job: Mapping[str, Any], *, application_id: str,
                     idempotency_key: str) -> Mapping[str, Any]:
        """Explicitly select an unchanged standard without generation or normalization."""
        snapshot=self._job(job); snapshot.validate()
        replay=self.service.get_selection_by_idempotency_key(idempotency_key)
        if replay:
            if str(replay['application_id']) != application_id:
                raise ResumeConflictError('selection command belongs to another application')
            return self.get_application_workspace(application_id)
        self._require_preparing_application(application_id,snapshot)
        # Separate read/evaluation adapter ensures even a configured provider cannot
        # be called as a side effect of choosing the fixed standard.
        local=ResumeLabProductionGateway(self.service,self.artifacts,
                                        application_db=self.application_db,model=None,toolchain=None,
                                        application_gateway=self.application_gateway)
        ranked,_=local._rank_standards(snapshot)
        viable=[r for r in ranked if r.get('artifact_id') and r.get('evaluation_id')]
        if not viable:
            return self._blocked('no_parse_safe_standard',application_id=application_id,ranked=ranked,job=snapshot)
        selected=min(viable,key=lambda r:(int(r['manual_rank']),str(r['standard_version_id'])))
        result=local.select_resume(application_id,job=job,artifact_id=str(selected['artifact_id']),
                                  evaluation_id=str(selected['evaluation_id']),idempotency_key=idempotency_key)
        return {**self.get_application_workspace(application_id),**result,
                'status':'standard_selected','resume_mode':'standard','ranked_standards':ranked}

    def import_standard(
        self, name: str, manual_rank: int, tex_source: str
    ) -> Mapping[str, Any]:
        """Compile, parse, and optionally normalize one user-authored TeX resume."""

        version, normalization_status, normalization_error = (
            self._build_standard_version(tex_source)
        )
        saved = self.service.create_standard(
            name,
            manual_rank,
            version,
            actor_kind="user",
        )
        return {
            **self._public_standard(saved["standard"], saved["version"]),
            "normalization_status": normalization_status,
            "normalization_error": normalization_error,
        }

    def update_standard(
        self, standard_id: str, tex_source: str
    ) -> Mapping[str, Any]:
        """Activate a new immutable version under an existing standard identity."""

        validate_identifier(standard_id, "standard_id")
        # Resolve the identity before compiling so a typo cannot create an orphaned
        # managed PDF. The store remains authoritative for active/inactive status.
        self.service.store.get_standard(standard_id)
        version, normalization_status, normalization_error = (
            self._build_standard_version(tex_source)
        )
        saved_version = self.service.add_standard_version(
            standard_id,
            version,
            actor_kind="user",
            activate=True,
        )
        standard = self.service.store.get_standard(standard_id)
        if str(standard.get("active_version_id") or "") != str(
            saved_version.get("version_id") or ""
        ):
            raise ResumeConflictError("new standard version was not activated")
        return {
            **self._public_standard(standard, saved_version),
            "normalization_status": normalization_status,
            "normalization_error": normalization_error,
        }

    def _register_standard(
        self,
        standard: Mapping[str, Any],
        version: Mapping[str, Any],
        job: JobSnapshot,
        graph: RequirementGraph,
        clauses: Sequence[Mapping[str, Any]],
        extraction_source: str,
    ) -> Mapping[str, Any]:
        imported = version.get("import_metadata")
        if not isinstance(imported, Mapping) or not imported.get("parse_safe"):
            raise ResumeBoundaryError("standard resume has no parse-safe imported PDF")
        self.artifacts.read_pdf(
            str(imported["managed_relative_path"]), str(imported["pdf_sha256"])
        )
        model_provenance = self._model_provenance()
        scoring_mode = "model_eligible" if self._model_calls_allowed() else "deterministic"
        cached = self.service.find_standard_artifact(
            str(version["version_id"]), job.fingerprint, graph.fingerprint
        )
        if cached is not None:
            cached_metadata = cached.get("metadata") or {}
            evaluation_id = cached_metadata.get("evaluation_id")
            if (
                cached_metadata.get("scorer_revision") == SCORER_REVISION
                and cached_metadata.get("model_provenance") == model_provenance
                and (scoring_mode == "deterministic"
                     or cached_metadata.get("scoring_mode") == scoring_mode)
                and isinstance(evaluation_id, str)
                and self.service.store.has_artifact_evaluation(
                    str(cached["artifact_id"]), evaluation_id
                )
            ):
                score_data = {
                    key: cached_metadata[key]
                    for key in (
                        "evaluation_id",
                        "score",
                        "parsed_fit",
                        "requirement_evidence",
                        "search_visibility",
                        "screening_readiness",
                        "eligibility_status",
                        "scorer_revision",
                        "requirement_graph_fingerprint",
                        "criteria",
                    )
                }
                return {
                    **self._public_standard(standard, version),
                    **score_data,
                    "artifact_id": cached["artifact_id"],
                    "status": "ready",
                }
        evaluation = self._score(
            str(imported["parsed_text"]), graph, version.get("claims") or ()
        )
        score_data = _score_mapping(evaluation)
        metadata = {
            **score_data,
            "requirement_graph_fingerprint": graph.fingerprint,
            "requirement_clauses": list(clauses),
            "requirement_extraction": extraction_source,
            "standard_id": standard["standard_id"],
            "gateway_revision": GATEWAY_REVISION,
            "model_provenance": model_provenance,
            "scoring_mode": scoring_mode,
        }
        artifact = self.service.register_artifact(
            ArtifactInput(
                variant_kind=VariantKind.STANDARD,
                purpose=ResumePurpose.REAL_APPLICATION,
                job=job,
                tex_source=str(version["tex_source"]),
                intended_text=str(version["plain_text"]),
                claims=_claims(version["claims"]),
                managed_relative_path=str(imported["managed_relative_path"]),
                pdf_sha256=str(imported["pdf_sha256"]),
                parsed_text=str(imported["parsed_text"]),
                parse_fidelity=float(imported["parse_fidelity"]),
                parse_safe=True,
                generator_revision="handwritten-import-v1",
                base_version_id=str(version["version_id"]),
                metadata=metadata,
            )
        )
        self.service.store.put_evaluation(
            str(artifact["artifact_id"]), graph, evaluation
        )
        return {
            **self._public_standard(standard, version),
            **score_data,
            "artifact_id": artifact["artifact_id"],
            "evaluation_id": evaluation.cache_key,
            "status": "ready",
        }

    def _rank_standards(
        self, job: JobSnapshot
    ) -> tuple[list[Mapping[str, Any]], RequirementGraph]:
        graph, clauses, source = self._extract_graph(job)
        ranked = []
        for standard in self.service.list_active_standards():
            version = self.service.store.get_standard_version(
                str(standard["active_version_id"])
            )
            try:
                value = self._register_standard(
                    standard, version, job, graph, clauses, source
                )
            except Exception as exc:
                _raise_if_runpod_reconciliation(exc)
                value = {
                    **self._public_standard(standard, version),
                    "score": None,
                    "status": "error",
                    "error_code": _safe_error_code(exc),
                }
            ranked.append(value)
        ranked.sort(
            key=lambda item: (
                item.get("score") is None,
                -float(item.get("score") or 0),
                int(item["manual_rank"]),
                str(item["name"]).casefold(),
                str(item["standard_version_id"]),
            )
        )
        for index, item in enumerate(ranked, 1):
            item["rank"] = index
            item["primary"] = index == 1 and item.get("score") is not None
        return ranked, graph

    def _cached_ranked_standards(
        self, run: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        """Rebuild a run's handwritten comparison from persisted score artifacts."""

        if run.get("source_mode") == "career_profile":
            return []
        frozen = run.get("ranked_standards")
        if isinstance(frozen, list):
            return [dict(item) for item in frozen if isinstance(item, Mapping)]

        analysis = run.get("requirement_analysis") or {}
        graph_fingerprint = str(
            analysis.get("requirement_graph_fingerprint") or ""
        )
        job = JobSnapshot(**run["job_snapshot"])
        standards = list(self.service.list_active_standards())
        selected_version_id = str(run["base_version_id"])
        if not any(
            str(item.get("active_version_id") or "") == selected_version_id
            for item in standards
        ):
            # Keep the immutable run base visible even if the user later archives the
            # standard or activates a newer version.
            try:
                version = self.service.store.get_standard_version(selected_version_id)
                historical = dict(
                    self.service.store.get_standard(str(version["standard_id"]))
                )
                historical["active"] = False
                historical["active_version_id"] = selected_version_id
                standards.append(historical)
            except ResumeLabError:
                pass

        ranked: list[Mapping[str, Any]] = []
        for standard in standards:
            version = self.service.store.get_standard_version(
                str(standard["active_version_id"])
            )
            artifact = self.service.find_standard_artifact(
                str(version["version_id"]), job.fingerprint, graph_fingerprint
            )
            if artifact is None:
                value = {
                    **self._public_standard(standard, version),
                    "score": None,
                    "status": "not_scored_for_run",
                }
            else:
                value = {
                    **self._public_standard(standard, version),
                    **self._artifact_public(artifact),
                    "status": "ready",
                }
            ranked.append(value)
        ranked.sort(
            key=lambda item: (
                item.get("score") is None,
                -float(item.get("score") or 0),
                int(item["manual_rank"]),
                str(item["name"]).casefold(),
                str(item["standard_version_id"]),
            )
        )
        for index, item in enumerate(ranked, 1):
            item["rank"] = index
            item["primary"] = index == 1 and item.get("score") is not None
        return ranked

    def _blocked(
        self,
        reason: str,
        *,
        application_id: str,
        ranked: Sequence[Mapping[str, Any]] = (),
        job: Optional[JobSnapshot] = None,
    ) -> Mapping[str, Any]:
        return {
            "status": "blocked_setup",
            "reason": reason,
            "ranked_standards": list(ranked),
            "comparisons": [],
            "job_title": job.title if job else "",
            **self._application_authority(application_id),
        }

    def prepare(
        self,
        job: Mapping[str, Any],
        *,
        application_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        snapshot = self._job(job)
        snapshot.validate()
        validate_identifier(application_id, "application_id")
        replay = self.service.get_run_by_idempotency_key(idempotency_key)
        if replay is not None:
            self._validate_run_replay(replay, application_id, snapshot)
            return self.get_run_result(str(replay["run_id"]))
        if self.career_store.get_profile().get("approved_revision_id"):
            return self.prepare_from_career_profile(job, application_id=application_id, idempotency_key=idempotency_key)
        self._require_preparing_application(application_id, snapshot)
        try:
            ranked, graph = self._rank_standards(snapshot)
        except RequirementGraphLimitError:
            return self._blocked(
                "job_description_too_complex",
                application_id=application_id,
                job=snapshot,
            )
        viable = [item for item in ranked if item.get("score") is not None]
        if not viable:
            reason = "no_standard_resume" if not ranked else "no_parse_safe_standard"
            return self._blocked(
                reason,
                application_id=application_id,
                ranked=ranked,
                job=snapshot,
            )
        winner = viable[0]
        version = self.service.store.get_standard_version(
            str(winner["standard_version_id"])
        )
        if not _has_current_normalization(version):
            return self._blocked(
                "needs_normalization",
                application_id=application_id,
                ranked=ranked,
                job=snapshot,
            )
        try:
            self._require_generation_ready()
        except ResumeSetupError:
            return self._blocked(
                "generation_not_configured",
                application_id=application_id,
                ranked=ranked,
                job=snapshot,
            )
        standard_artifact = self.service.get_artifact(
            str(winner["artifact_id"]), include_content=True
        )
        standard_metadata = standard_artifact.get("metadata") or {}
        # Ranking and parsing may take long enough for submission in another tab.
        # Hold the application writer lock across both sidecar creation and queue
        # insertion, so either this command wins in full or submission wins first.
        def create_and_dispatch(
            application_connection: sqlite3.Connection,
        ) -> Mapping[str, Any]:
            created = self.service.create_run(
                application_id,
                snapshot,
                idempotency_key,
                standard_id=str(winner["standard_id"]),
                expected_base_version_id=str(winner["standard_version_id"]),
                ranked_standards=ranked,
                requirement_graph=graph,
                requirement_clauses=tuple(
                    standard_metadata.get("requirement_clauses") or ()
                ),
                requirement_extraction=str(
                    standard_metadata.get("requirement_extraction")
                    or "deterministic_fallback"
                ),
            )
            if (created.get("requirement_analysis") or {}).get(
                "requirement_graph_fingerprint"
            ) != graph.fingerprint:
                raise ResumeConflictError("resume run requirement analysis changed")
            self._enqueue_run_while_locked(application_connection, created)
            return created

        run = self._while_application_preparing(
            application_id, snapshot, create_and_dispatch
        )
        return self._run_view(run, ranked_standards=ranked)

    def start_run(
        self,
        job: Mapping[str, Any],
        *,
        application_id: str,
        standard_version_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        validate_identifier(standard_version_id, "standard_version_id")
        snapshot = self._job(job)
        snapshot.validate()
        validate_identifier(application_id, "application_id")
        replay = self.service.get_run_by_idempotency_key(idempotency_key)
        if replay is not None:
            self._validate_run_replay(
                replay,
                application_id,
                snapshot,
                expected_base_version_id=standard_version_id,
            )
            return self.get_run_result(str(replay["run_id"]))
        self._require_preparing_application(application_id, snapshot)
        try:
            ranked, graph = self._rank_standards(snapshot)
        except RequirementGraphLimitError:
            return self._blocked(
                "job_description_too_complex",
                application_id=application_id,
                job=snapshot,
            )
        viable = [item for item in ranked if item.get("score") is not None]
        if not viable or viable[0]["standard_version_id"] != standard_version_id:
            raise ResumeConflictError(
                "derived comparisons must use the highest-scoring active standard"
            )
        version = self.service.store.get_standard_version(standard_version_id)
        if not _has_current_normalization(version):
            return self._blocked(
                "needs_normalization",
                application_id=application_id,
                ranked=ranked,
                job=snapshot,
            )
        self._require_generation_ready()
        standard_artifact = self.service.get_artifact(
            str(viable[0]["artifact_id"]), include_content=True
        )
        standard_metadata = standard_artifact.get("metadata") or {}
        def create_and_dispatch(
            application_connection: sqlite3.Connection,
        ) -> Mapping[str, Any]:
            created = self.service.create_run(
                application_id,
                snapshot,
                idempotency_key,
                standard_id=str(viable[0]["standard_id"]),
                expected_base_version_id=standard_version_id,
                ranked_standards=ranked,
                requirement_graph=graph,
                requirement_clauses=tuple(
                    standard_metadata.get("requirement_clauses") or ()
                ),
                requirement_extraction=str(
                    standard_metadata.get("requirement_extraction")
                    or "deterministic_fallback"
                ),
            )
            self._enqueue_run_while_locked(application_connection, created)
            return created

        run = self._while_application_preparing(
            application_id, snapshot, create_and_dispatch
        )
        return self._run_view(run, ranked_standards=ranked)

    def _artifact_public(self, artifact: Mapping[str, Any]) -> Mapping[str, Any]:
        metadata = artifact.get("metadata") or {}
        return {
            "comparison_kind": artifact["variant_kind"],
            "source_mode": artifact.get("source_mode", "standard"),
            "template_version": metadata.get("template_version"),
            "artifact_id": artifact["artifact_id"],
            "evaluation_id": metadata.get("evaluation_id"),
            "score": metadata.get("score"),
            "parsed_fit": metadata.get("parsed_fit"),
            "requirement_evidence": metadata.get("requirement_evidence"),
            "search_visibility": metadata.get("search_visibility"),
            "screening_readiness": metadata.get("screening_readiness"),
            "eligibility_status": metadata.get("eligibility_status"),
            "criteria": metadata.get("criteria", []),
            "research_only": artifact["purpose"]
            == ResumePurpose.SYNTHETIC_RESEARCH.value,
            "changes": metadata.get("changes", []),
            "added_keywords": metadata.get("added_keywords", []),
        }

    def _selection_artifact_public(
        self, artifact: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Expose only the human identity of a selected artifact's frozen base."""

        visible = dict(self._artifact_public(artifact))
        if artifact.get("source_mode") == "career_profile":
            metadata = artifact.get("metadata") or {}
            return {**visible, "standard_id": None, "standard_version_id": None,
                "name": "Career profile · tailored resume", "composition_id": artifact["composition_id"],
                "profile_revision_id": metadata.get("profile_revision_id")}

        if artifact["variant_kind"] not in {
            VariantKind.STANDARD.value,
            VariantKind.GROUNDED_REWRITE.value,
        }:
            return visible
        version = self.service.store.get_standard_version(
            str(artifact["base_version_id"])
        )
        standard = self.service.store.get_standard(str(version["standard_id"]))
        visible.update(
            {
                "standard_id": standard["standard_id"],
                "standard_version_id": version["version_id"],
                "name": standard["name"],
            }
        )
        return visible

    @staticmethod
    def _validate_run_replay(
        run: Mapping[str, Any],
        application_id: str,
        job: JobSnapshot,
        *,
        expected_base_version_id: Optional[str] = None,
    ) -> None:
        if (
            str(run.get("application_id")) != application_id
            or canonical_json(run.get("job_snapshot")) != canonical_json(asdict(job))
            or (
                expected_base_version_id is not None
                and str(run.get("base_version_id")) != expected_base_version_id
            )
        ):
            raise ResumeConflictError(
                "resume run idempotency key belongs to different input"
            )

    def _run_view(
        self,
        run: Mapping[str, Any],
        *,
        ranked_standards: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Mapping[str, Any]:
        approval = run.get("grounded_approval")
        comparisons = []
        for item in run["items"]:
            candidate: dict[str, Any] = {
                "comparison_kind": item["variant_kind"],
                "status": item["status"],
                "research_only": item["purpose"]
                == ResumePurpose.SYNTHETIC_RESEARCH.value,
                "error_code": item.get("error") or None,
            }
            if item.get("artifact_id"):
                artifact = self.service.get_artifact(str(item["artifact_id"]))
                candidate.update(self._artifact_public(artifact))
            if item["variant_kind"] == VariantKind.GROUNDED_REWRITE.value:
                approved = bool(
                    approval and approval.get("artifact_id") == item.get("artifact_id")
                )
                candidate["approved"] = approved
                candidate["approved_at"] = (
                    approval.get("approved_at") if approved else None
                )
                if approved:
                    candidate["status"] = "approved"
            comparisons.append(candidate)
        frozen_rankings = run.get("ranked_standards")
        visible_rankings = (
            frozen_rankings
            if ranked_standards is None and isinstance(frozen_rankings, list)
            else ranked_standards or ()
        )
        return {
            "run_id": run["run_id"],
            "status": run["status"],
            "reason": run.get("error") or None,
            "job_title": run["job_snapshot"]["title"],
            "selected_standard_id": run["selected_standard_id"],
            "selected_standard_version_id": run["base_version_id"],
            "ranked_standards": list(visible_rankings),
            "comparisons": comparisons,
            **self._application_authority(str(run["application_id"])),
            "gateway_revision": GATEWAY_REVISION,
            **(self._career_run_details(run) if run.get("source_mode") == "career_profile" else {}),
        }

    def get_run_result(self, run_id: str) -> Mapping[str, Any]:
        run = self.service.get_run(run_id)
        if run["status"] in {"queued", "running"} and self._enqueue_callback:
            job = JobSnapshot(**run["job_snapshot"])
            try:
                self._while_application_preparing(
                    str(run["application_id"]),
                    job,
                    lambda application_connection: self._enqueue_run_while_locked(
                        application_connection, run
                    ),
                )
            except _ApplicationStateConflict:
                run = self.service.cancel_active_run_for_application_phase(
                    run_id, error="application_not_preparing"
                )
            else:
                # ``create_run`` and the application work queue live in different
                # SQLite databases, so a process can die after the former commits and
                # before the latter does.  Polling is a durable reconciliation path,
                # not merely a read.  The phase check and idempotent enqueue share one
                # writer transaction, preventing work from appearing after submission.
                run = self.service.get_run(run_id)
        return self._run_view(
            run, ranked_standards=self._cached_ranked_standards(run)
        )

    def retry_run(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        reconciliation_acknowledged: bool = False,
    ) -> Mapping[str, Any]:
        validate_identifier(run_id, "run_id")
        validate_identifier(idempotency_key, "idempotency_key")
        if not isinstance(reconciliation_acknowledged, bool):
            raise ResumeBoundaryError(
                "reconciliation acknowledgment must be a boolean"
            )
        replay = self.service.get_retry_command(idempotency_key)
        if replay is not None:
            if str(replay["run_id"]) != run_id:
                raise ResumeConflictError(
                    "retry idempotency key belongs to a different run"
                )
            result_run_id = str(replay.get("result_run_id") or run_id)
            # The retry command and application work queue live in separate SQLite
            # databases.  A crash can commit the successor before its delivery is
            # enqueued, so an exact replay must take the normal reconciliation path.
            return self.get_run_result(result_run_id)
        current = self.service.get_run(run_id)
        if current.get("source_mode") == "career_profile" and current.get("run_role") == "primary":
            self._require_career_ready()
        else:
            self._require_generation_ready()
        application_id = str(current["application_id"])
        job = JobSnapshot(**current["job_snapshot"])

        def retry_while_locked(
            application_connection: sqlite3.Connection,
        ) -> Mapping[str, Any]:
            if current["status"] == "running" and reconciliation_acknowledged:
                from ..inference.usage import resume_restart_was_reconciled
                if not resume_restart_was_reconciled(application_connection, run_id):
                    raise ResumeConflictError("running resume work requires audited inference reconciliation")
                self.service.cancel_active_run_for_application_phase(run_id, error=RUNPOD_RECONCILIATION_ERROR)
            run = self.service.retry_run(
                run_id,
                idempotency_key,
                actor_kind="user",
                reconciliation_acknowledged=reconciliation_acknowledged,
            )
            if (
                not run.get("retry_replayed")
                and run["status"] in {"queued", "running"}
            ):
                self._enqueue_run_while_locked(application_connection, run)
            return run

        run = self._while_application_preparing(
            application_id, job, retry_while_locked
        )
        return self._run_view(run)

    def approve_run(
        self,
        run_id: str,
        *,
        comparison_kind: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if comparison_kind != VariantKind.GROUNDED_REWRITE.value:
            raise ResumeBoundaryError("only the grounded rewrite can be approved")
        validate_identifier(idempotency_key, "idempotency_key")
        run = self.service.get_run(run_id)
        item = next(
            value
            for value in run["items"]
            if value["variant_kind"] == VariantKind.GROUNDED_REWRITE.value
        )
        if not item.get("artifact_id"):
            raise ResumeConflictError("grounded rewrite is not ready")
        replay = self.service.get_approval_by_idempotency_key(idempotency_key)
        if replay is not None:
            if (
                str(replay["run_id"]) != run_id
                or str(replay["artifact_id"]) != str(item["artifact_id"])
            ):
                raise ResumeConflictError(
                    "approval idempotency key belongs to different content"
                )
            return self._run_view(run)
        if self.application_db is None or not self.application_db.is_file():
            raise _ApplicationLedgerUnavailable("application ledger is unavailable")
        job = JobSnapshot(**run["job_snapshot"])
        # Hold the authoritative application writer lock until the sidecar approval
        # commits.  Submission therefore either wins first (and approval is rejected)
        # or waits until this exact approval is durably recorded.
        try:
            with self._application_preparing_connection(str(run["application_id"]), job):
                self.service.approve_run(
                    run_id,
                    str(item["artifact_id"]),
                    idempotency_key,
                    actor_kind="user",
                )
        except (OSError, sqlite3.Error) as exc:
            raise _ApplicationLedgerUnavailable(
                "application ledger is unavailable"
            ) from exc
        return self._run_view(self.service.get_run(run_id))

    def select_resume(
        self,
        application_id: str,
        *,
        job: Mapping[str, Any],
        artifact_id: str,
        evaluation_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        artifact = self.service.get_artifact(artifact_id, include_content=True)
        metadata = artifact.get("metadata") or {}
        if metadata.get("evaluation_id") != evaluation_id:
            raise ResumeConflictError("evaluation does not belong to the artifact")
        evaluation = self.service.store.get_cached_evaluation(evaluation_id)
        if (
            not evaluation
            or not self.service.store.has_artifact_evaluation(
                artifact_id, evaluation_id
            )
            or evaluation.get("artifact_text_sha256")
            != sha256_text(str(artifact["parsed_text"]))
        ):
            raise ResumeConflictError("artifact was not evaluated for this application")
        replay = self.service.get_selection_by_idempotency_key(idempotency_key)
        if replay is not None:
            if (
                str(replay["application_id"]) != application_id
                or str(replay["artifact_id"]) != artifact_id
            ):
                raise ResumeConflictError(
                    "selection idempotency key belongs to different content"
                )
            return {
                "selection": {
                    **replay,
                    **self._selection_artifact_public(artifact),
                }
            }
        current_job = self._job(job)
        current_job.validate()
        if str(artifact["job_fingerprint"]) != current_job.fingerprint:
            raise ResumeConflictError(
                "resume artifact belongs to a different job snapshot"
            )
        if self.application_db is None or not self.application_db.is_file():
            raise _ApplicationLedgerUnavailable("application ledger is unavailable")
        artifact_job = JobSnapshot(
            ats=current_job.ats,
            job_id=current_job.job_id,
            title="selection-bound-artifact",
            description="selection-bound-artifact",
            employer=current_job.employer,
        )
        # A RESERVED writer transaction serializes this check with application phase
        # changes.  Submission either commits first (and selection is rejected) or
        # waits until this exact selection has committed in the sidecar.
        try:
            with self._application_preparing_connection(application_id, artifact_job):
                # The database binds the expected digest, but selection is the last
                # boundary before an artifact can be used in an application.  Verify
                # the managed bytes while the phase lock is held so missing or altered
                # PDF content can never become the selected resume.
                self.artifacts.read_pdf(
                    str(artifact["managed_relative_path"]),
                    str(artifact["pdf_sha256"]),
                )
                selection = self.service.select_for_application(
                    application_id,
                    artifact_id,
                    idempotency_key,
                    actor_kind="user",
                )
        except (OSError, sqlite3.Error) as exc:
            raise _ApplicationLedgerUnavailable(
                "application ledger is unavailable"
            ) from exc
        return {
            "selection": {
                **selection,
                **self._selection_artifact_public(artifact),
            }
        }

    def get_selection(self, application_id: str) -> Mapping[str, Any]:
        selection = self.service.current_application_selection(application_id)
        if selection is None:
            return {"selection": None}
        artifact = self.service.get_artifact(str(selection["artifact_id"]))
        return {
            "selection": {
                **selection,
                **self._selection_artifact_public(artifact),
            }
        }

    def get_autofill_resume(self, application_id: Optional[str] = None) -> Optional[ResumeArtifactContent]:
        """Read a selected PDF or preferred active standard; never generate or select."""
        if application_id:
            selection = self.get_selection(application_id).get("selection")
            if selection:
                return self.get_artifact(str(selection["artifact_id"]))
        standards = sorted(self.service.list_active_standards(),
                           key=lambda s: (int(s["manual_rank"]), str(s["active_version_id"])))
        for standard in standards:
            version = self.service.store.get_standard_version(str(standard["active_version_id"]))
            metadata = version["import_metadata"]
            if not metadata.get("parse_safe"):
                continue
            verified = self.artifacts.read_pdf(str(metadata["managed_relative_path"]),
                                               str(metadata["pdf_sha256"]))
            return ResumeArtifactContent(
                artifact_id=str(standard["active_version_id"]), filename="resume.pdf",
                content_type=verified.content_type, content=verified.content, sha256=verified.sha256)
        return None

    def match_uploaded_resume(self, digest: str) -> Optional[Mapping[str, Any]]:
        """Match bytes selected by the user, without attributing an assumed resume."""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return None
        from .store import connect as resume_connect
        with resume_connect(self.service.store.db_path) as con:
            for row in con.execute("SELECT v.version_id,v.import_metadata_json,s.standard_id,s.name FROM resume_standard_versions v JOIN resume_standards s USING(standard_id) ORDER BY v.created_at DESC"):
                metadata = json.loads(row["import_metadata_json"] or "{}")
                if metadata.get("pdf_sha256") == digest and metadata.get("parse_safe"):
                    self.artifacts.read_pdf(str(metadata["managed_relative_path"]), digest)
                    return {"decision": "matched_upload", "standard_version_id": row["version_id"],
                            "standard_id": row["standard_id"], "name": row["name"], "sha256": digest}
        return None

    def get_application_resume_content(self, application_id: str) -> Mapping[str, Any]:
        from .agent_context import application_resume_content

        return application_resume_content(self, application_id)

    def get_application_workspace(
        self, application_id: str
    ) -> Mapping[str, Any]:
        """Return the latest durable comparison so the dashboard can reopen it."""

        validate_identifier(application_id, "application_id")
        authority = self._application_authority(application_id)
        selection = self.get_selection(application_id)["selection"]
        run = self.service.get_latest_application_run(application_id)
        if run is None:
            return {
                "application_id": application_id,
                "run_id": None,
                "status": "not_started",
                "reason": "no_resume_comparison",
                "ranked_standards": self.list_standards()["standards"],
                "comparisons": [],
                "selection": selection,
                **authority,
                "gateway_revision": GATEWAY_REVISION,
            }
        return {
            **self._run_view(
                run, ranked_standards=self._cached_ranked_standards(run)
            ),
            "application_id": application_id,
            "selection": selection,
            **authority,
        }

    def get_standard_document(
        self, version_id: str, *, expected_sha256: Optional[str] = None
    ) -> ResumeArtifactContent:
        """Read the PDF saved for this exact immutable version; never generate it."""
        version = self.service.store.get_standard_version(version_id)
        imported = version.get("import_metadata") or {}
        if not imported.get("managed_relative_path") or not imported.get("pdf_sha256"):
            raise ResumeNotFoundError("a PDF is not recorded for this resume version")
        if expected_sha256 is not None and imported["pdf_sha256"] != expected_sha256:
            raise ResumeBoundaryError("saved resume does not match the recorded document digest")
        verified = self.artifacts.read_pdf(
            str(imported["managed_relative_path"]), str(imported["pdf_sha256"])
        )
        if verified.namespace != ArtifactNamespace.REAL:
            raise ResumeBoundaryError("saved resume documents must use real document storage")
        return ResumeArtifactContent(
            artifact_id=version_id,
            filename="saved-resume.pdf",
            content_type=verified.content_type,
            content=verified.content,
            sha256=verified.sha256,
        )

    def get_artifact(self, artifact_id: str) -> ResumeArtifactContent:
        artifact = self.service.get_artifact(artifact_id)
        verified = self.artifacts.read_pdf(
            str(artifact["managed_relative_path"]), str(artifact["pdf_sha256"])
        )
        kind = str(artifact["variant_kind"]).replace("_", "-")
        return ResumeArtifactContent(
            artifact_id=artifact_id,
            filename=f"resume-{kind}.pdf",
            content_type=verified.content_type,
            content=verified.content,
            sha256=verified.sha256,
        )

    def compare_for_job(self, ats: str, job_id: str) -> Mapping[str, Any]:
        validate_identifier(str(ats), "ats")
        validate_identifier(str(job_id), "job_id")
        matches = list(
            self.service.list_runs_for_job(str(ats).lower(), str(job_id), limit=10)
        )
        # A sequence of research retries must not push the primary resume outside
        # the bounded history window used for the compact Hermes summary.
        from .store import connect as resume_connect
        with resume_connect(self.service.store.db_path) as connection:
            primary = connection.execute(
                "SELECT r.* FROM resume_runs r JOIN resume_run_order o ON o.run_id=r.run_id "
                "WHERE r.ats=? AND r.job_id=? AND r.run_role='primary' ORDER BY o.run_seq DESC LIMIT 1",
                (str(ats).lower(), str(job_id)),
            ).fetchone()
            latest = self.service.store._run(connection, primary) if primary else None
        latest_view = self._run_view(latest) if latest else None
        visible_comparisons = list(latest_view["comparisons"]) if latest_view else []
        if latest_view and latest_view.get("research_runs"):
            visible_comparisons.extend(latest_view["research_runs"][0]["comparisons"])
        standards = (
            self._cached_ranked_standards(latest) if latest is not None else []
        )
        return {
            "ats": str(ats).lower(),
            "job_id": str(job_id),
            "runs": [
                {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "created_at": run["created_at"],
                    "selected_standard_version_id": run["base_version_id"],
                }
                for run in matches
            ],
            "ranked_standards": [
                self._hermes_candidate(item, include_gaps=index == 0)
                for index, item in enumerate(standards)
            ],
            "comparisons": [
                self._hermes_candidate(item, include_gaps=True)
                for item in visible_comparisons
            ],
        }

    @staticmethod
    def _hermes_candidate(
        value: Mapping[str, Any], *, include_gaps: bool
    ) -> Mapping[str, Any]:
        """Serialize only compact, non-resume-text comparison facts for Hermes."""

        criteria = [
            item
            for item in value.get("criteria", ())
            if isinstance(item, Mapping)
        ]
        contradictions = [
            item for item in criteria if item.get("status") == "contradicted"
        ]
        gaps = [
            item
            for item in criteria
            if item.get("status") not in {"met", "contradicted"}
        ]
        gaps.sort(
            key=lambda item: (
                -float(item.get("weight") or 0),
                str(item.get("requirement_id") or ""),
            )
        )
        compact: dict[str, Any] = {
            key: value.get(key)
            for key in (
                "comparison_kind",
                "standard_id",
                "standard_version_id",
                "name",
                "rank",
                "status",
                "score",
                "parsed_fit",
                "requirement_evidence",
                "search_visibility",
                "screening_readiness",
                "eligibility_status",
                "research_only",
                "approved",
            )
            if value.get(key) is not None
        }
        compact["contradiction_count"] = len(contradictions)
        compact["gap_count"] = len(gaps)
        if include_gaps:
            compact["top_gaps"] = [
                {
                    "requirement_id": item.get("requirement_id"),
                    "priority": item.get("priority"),
                    "status": item.get("status"),
                    "weight": item.get("weight"),
                    "requirement": str(item.get("source_text") or "")[:240],
                }
                for item in (contradictions + gaps)[:3]
            ]
        return compact

    def _analysis_for_run(
        self, run: Mapping[str, Any], job: JobSnapshot
    ) -> RequirementGraph:
        analysis = run.get("requirement_analysis")
        if not isinstance(analysis, Mapping):
            raise ResumeConflictError("run has no immutable requirement analysis")
        clauses = analysis.get("clauses") or []
        if not isinstance(clauses, list):
            raise ResumeConflictError("run requirement analysis is invalid")
        graph = requirement_graph_from_clauses(job, clauses, include_fallback=True)
        if graph.fingerprint != analysis.get("requirement_graph_fingerprint"):
            raise ResumeConflictError("run requirement analysis changed")
        return graph

    @staticmethod
    def _source_claims(
        version: Mapping[str, Any], _graph: RequirementGraph
    ) -> list[Mapping[str, Any]]:
        imported = version.get("import_metadata") or {}
        rows = imported.get("normalization_claims") or []
        if rows:
            claims = [dict(item) for item in rows]
        else:
            claims = [
                {
                    "claim_id": item["claim_id"],
                    "text": item["text"],
                    "origin": item.get("origin"),
                }
                for item in version.get("claims") or ()
            ]
        return curated_source_claims(claims)

    @staticmethod
    def _guarded_terms(graph: RequirementGraph) -> list[str]:
        return sorted(
            {
                term.casefold()
                for requirement in graph.requirements
                for group in requirement.term_groups
                for term in group
            }
        )

    @staticmethod
    def _notice_survived(text: str) -> bool:
        expected = normalize_ats_tokens(SYNTHETIC_NOTICE)
        actual = normalize_ats_tokens(text)
        width = len(expected)
        return bool(width) and any(
            actual[index : index + width] == expected
            for index in range(max(0, len(actual) - width + 1))
        )

    @staticmethod
    def _optimization_feedback(
        content: Mapping[str, Any], graph: RequirementGraph
    ) -> Mapping[str, Any]:
        rendered = render_resume_tex(content)
        result = score_ats_proxy(rendered.intended_text, graph)
        missing = [
            {
                "requirement_id": item.requirement_id,
                "source_text": item.source_text,
                "status": item.status.value,
                "weight": item.weight,
                "missing_term_groups": [
                    list(group) for group in item.missing_term_groups
                ],
            }
            for item in result.criteria
            if item.weight > 0 and item.status is not EvidenceStatus.MET
        ]
        missing.sort(key=lambda item: (-float(item["weight"]), item["requirement_id"]))
        return {"score": result.parsed_fit, "remaining_gaps": missing[:20]}

    def _generate_output(
        self,
        kind: VariantKind,
        job: JobSnapshot,
        graph: RequirementGraph,
        generation_seed: int,
        standard_candidates: Sequence[Mapping[str, Any]],
        source_claims: Sequence[Mapping[str, Any]],
        guarded_terms: Sequence[str],
    ) -> Mapping[str, Any]:
        job_value: dict[str, Any] = {
            **asdict(job),
            "generation_seed": generation_seed,
        }
        first = self.model.generate_variant(
            kind.value,
            job_value,
            standard_candidates,
            source_claims,
            guarded_terms,
            generation_seed=generation_seed,
            optimization_input=None,
        )
        validation_base = (
            standard_candidates[0]
            if kind
            in {VariantKind.GROUNDED_REWRITE, VariantKind.STANDARD_EXAGGERATED}
            else None
        )
        first = validate_variant_output(
            first,
            kind.value,
            validation_base,
            source_claims,
            guarded_terms,
        )
        if kind is not VariantKind.KEYWORD_ADVERSARIAL:
            return first
        first_feedback = self._optimization_feedback(first["content"], graph=graph)
        if float(first_feedback["score"]) >= 99.5:
            return first
        optimization_input = {
            "optimization_pass": 2,
            "prior_score": first_feedback["score"],
            "prior_content": first["content"],
            "remaining_gaps": [
                {
                    "requirement_id": item["requirement_id"],
                    "source_text": item["source_text"],
                    "status": item["status"],
                    "weight": item["weight"],
                    "missing_term_groups": item["missing_term_groups"],
                }
                for item in first_feedback["remaining_gaps"][:20]
            ],
        }
        try:
            second = self.model.generate_variant(
                kind.value,
                job_value,
                standard_candidates,
                source_claims,
                guarded_terms,
                generation_seed=generation_seed,
                optimization_input=optimization_input,
            )
            second = validate_variant_output(
                second,
                kind.value,
                validation_base,
                source_claims,
                guarded_terms,
            )
            second_feedback = self._optimization_feedback(
                second["content"], graph=graph
            )
        except Exception as exc:
            _raise_if_runpod_reconciliation(exc)
            return first
        return second if second_feedback["score"] >= first_feedback["score"] else first

    @staticmethod
    def _generated_claims(
        kind: VariantKind, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[ResumeClaim, ...]:
        synthetic = kind is not VariantKind.GROUNDED_REWRITE
        values = []
        for item in rows:
            if synthetic:
                origin = ClaimOrigin.SYNTHETIC_GENERATED
            else:
                # Model output is never reclassified as user-attested, even when it
                # reproduces a source claim verbatim.  Its real source IDs remain the
                # auditable authority for the wording.
                origin = ClaimOrigin.GENERATED_WORDING
            values.append(
                ResumeClaim(
                    claim_id=str(item["claim_id"]),
                    text=str(item["text"]),
                    origin=origin,
                    source_fact_ids=tuple(
                        str(value) for value in item.get("source_claim_ids", ())
                    ),
                )
            )
        return tuple(values)

    @staticmethod
    def _rewrite_metadata(
        output: Mapping[str, Any], source_claims: Sequence[Mapping[str, Any]]
    ) -> tuple[list[Mapping[str, Any]], list[str]]:
        source = {str(item["claim_id"]): str(item["text"]) for item in source_claims}
        changes = []
        terms: set[str] = set()
        for claim in output.get("claims") or ():
            terms.update(str(item) for item in claim.get("added_equivalent_terms", ()))
            cited = [
                source[value]
                for value in claim.get("source_claim_ids", ())
                if value in source
            ]
            rewritten = str(claim.get("text") or "")
            before = " ".join(cited)
            if before and before != rewritten:
                changes.append(
                    {
                        "path": str(claim.get("path") or ""),
                        "source": before,
                        "rewrite": rewritten,
                    }
                )
        return changes, sorted(terms)

    def _build_variant(
        self,
        run: Mapping[str, Any],
        job: JobSnapshot,
        graph: RequirementGraph,
        version: Mapping[str, Any],
        kind: VariantKind,
        assert_owned: Callable[[], None],
    ) -> str:
        normalized = version.get("normalized_content")
        if not _has_current_normalization(version) or not isinstance(
            normalized, Mapping
        ):
            raise ResumeConflictError("selected standard needs normalization")
        source_claims = self._source_claims(version, graph)
        standards = [
            {
                "standard_id": run["selected_standard_id"],
                "rank": 1,
                "content": dict(normalized),
            }
        ]
        seed = int(
            hashlib.sha256(f"{run['run_id']}\0{kind.value}".encode()).hexdigest()[:8],
            16,
        ) % 2_147_483_648
        output = self._generate_output(
            kind,
            job,
            graph,
            seed,
            standards,
            source_claims,
            self._guarded_terms(graph),
        )
        assert_owned()
        synthetic = kind is not VariantKind.GROUNDED_REWRITE
        build = self.toolchain.build(
            output["content"],
            allow_warning=False,
            visible_notice=SYNTHETIC_NOTICE if synthetic else "",
        )
        if synthetic and not (
            self._notice_survived(build.extracted.logical_text)
            and self._notice_survived(build.extracted.layout_text)
        ):
            raise ResumeBoundaryError(
                "synthetic research notice did not survive PDF extraction"
            )
        assert_owned()
        claims = self._generated_claims(kind, output["claims"])
        evaluation = self._score(
            build.extracted.logical_text, graph, [asdict(item) for item in claims]
        )
        assert_owned()
        score_data = _score_mapping(evaluation)
        changes, keywords = self._rewrite_metadata(output, source_claims)
        namespace = (
            ArtifactNamespace.REAL
            if kind is VariantKind.GROUNDED_REWRITE
            else ArtifactNamespace.RESEARCH
        )
        saved_pdf = self.artifacts.write_pdf(build.compiled.pdf_bytes, namespace)
        assert_owned()
        metadata = {
            **score_data,
            "gateway_revision": GATEWAY_REVISION,
            "template_version": getattr(build.rendered, "template_version", "career-ops-v1"),
            "research_only": synthetic,
            "visible_research_notice": SYNTHETIC_NOTICE if synthetic else None,
            "changes": changes if kind is VariantKind.GROUNDED_REWRITE else [],
            "added_keywords": keywords,
            "grounding_output": (
                output if kind is VariantKind.GROUNDED_REWRITE else None
            ),
            "grounding_validator_revision": (
                GROUNDING_VALIDATOR_REVISION
                if kind is VariantKind.GROUNDED_REWRITE
                else None
            ),
            "grounding_equivalence_revision": (
                GROUNDING_EQUIVALENCE_REVISION
                if kind is VariantKind.GROUNDED_REWRITE
                else None
            ),
            "normalization_validator_revision": NORMALIZATION_VALIDATOR_REVISION,
            "model_provenance": self._model_provenance(),
            "generation_seed": seed,
            "claim_provenance": [
                {
                    "claim_id": item.get("claim_id"),
                    "path": item.get("path"),
                    "origin": item.get("origin"),
                    "source_claim_ids": list(item.get("source_claim_ids", ())),
                    "added_equivalent_terms": list(
                        item.get("added_equivalent_terms", ())
                    ),
                }
                for item in output.get("claims") or ()
            ],
            "parser": build.extracted.parser,
            "parser_version": build.extracted.parser_version,
            "pages": build.extracted.pages,
            "content_stream_bytes": build.extracted.content_stream_bytes,
            "layout_text_sha256": sha256_text(build.extracted.layout_text),
            "fidelity": asdict(build.fidelity),
            "compiler": build.compiled.engine,
            "compiler_version": build.compiled.engine_version,
            "bundle_sha256": build.compiled.bundle_sha256,
        }
        artifact = self.service.register_artifact(
            ArtifactInput(
                variant_kind=kind,
                purpose=(
                    ResumePurpose.REAL_APPLICATION
                    if not synthetic
                    else ResumePurpose.SYNTHETIC_RESEARCH
                ),
                job=job,
                tex_source=build.rendered.tex_source,
                intended_text=build.rendered.intended_text,
                claims=claims,
                managed_relative_path=saved_pdf.managed_relative_path,
                pdf_sha256=saved_pdf.sha256,
                parsed_text=build.extracted.logical_text,
                parse_fidelity=_fidelity_scalar(build.fidelity),
                parse_safe=True,
                generator_revision=GATEWAY_REVISION,
                base_version_id=(
                    str(version["version_id"])
                    if kind
                    in {VariantKind.GROUNDED_REWRITE, VariantKind.STANDARD_EXAGGERATED}
                    else None
                ),
                study_id=("study_" + job.fingerprint[:24]) if synthetic else None,
                pair_id=("pair_" + str(run["run_id"])) if synthetic else None,
                treatment=kind.value if synthetic else "",
                generation_seed=seed if synthetic else None,
                metadata=metadata,
            )
        )
        assert_owned()
        self.service.store.put_evaluation(
            str(artifact["artifact_id"]), graph, evaluation
        )
        return str(artifact["artifact_id"])

    def handle_work(
        self, payload: Mapping[str, Any], context: TaskContext
    ) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) - {
            "run_id",
            "scheduled_for",
        }:
            raise ResumeBoundaryError("resume work payload is invalid")
        run_id = str(payload.get("run_id") or "")
        validate_identifier(run_id, "run_id")
        if run_id.startswith("import_"):
            return self._handle_career_import(run_id, context)
        run = self.service.get_run(run_id)
        if run.get("source_mode") == "career_profile" and run.get("run_role") == "primary":
            self._require_career_ready()
        else:
            self._require_generation_ready()
        if run["status"] == "succeeded":
            return {"run_id": run_id, "status": "succeeded", "resumed": True}
        if run["status"] == "failed":
            return {"run_id": run_id, "status": "failed", "resumed": True}
        from ..inference.usage import work_can_resume, UsageDeferred
        if (run["status"] == "running" and self._uses_queued_runpod_model()
                and not work_can_resume(self.application_db, str(getattr(context, "work_id", "")))):
            # A prior process may have died after Runpod accepted a job but before the
            # item result committed. The queue has no safe idempotency key for POST
            # /run, so recovery must stop rather than duplicate remote GPU work.
            failed = self.service.cancel_active_run_for_application_phase(
                run_id, error=RUNPOD_RECONCILIATION_ERROR
            )
            return {
                "run_id": run_id,
                "status": failed["status"],
                "reconciliation_required": True,
            }
        job = JobSnapshot(**run["job_snapshot"])
        try:
            self._require_preparing_application(str(run["application_id"]), job)
        except _ApplicationStateConflict:
            failed = self.service.cancel_active_run_for_application_phase(
                run_id, error="application_not_preparing"
            )
            return {"run_id": run_id, "status": failed["status"]}
        owner_token = "owner_" + content_sha256(
            {
                "run_id": run_id,
                "work_id": str(getattr(context, "work_id", run_id)),
                "work_attempt": int(getattr(context, "attempt", 0)),
            }
        )
        with _LeaseGuard(context.heartbeat) as lease:
            run = self.service.start_run(run_id, owner_token=owner_token)
            run_attempt = int(run["attempt"])
            job = JobSnapshot(**run["job_snapshot"])

            def assert_owned() -> None:
                lease.check(refresh=True)
                self.service.assert_run_owner(run_id, run_attempt, owner_token)

            def assert_permitted() -> None:
                assert_owned()
                self._require_preparing_application(
                    str(run["application_id"]), job
                )

            try:
                assert_permitted()
            except _ApplicationStateConflict:
                assert_owned()
                failed = self.service.complete_run(
                    run_id,
                    "failed",
                    error="application_not_preparing",
                    run_attempt=run_attempt,
                    owner_token=owner_token,
                )
                return {"run_id": run_id, "status": failed["status"]}
            version = (self.service.store.get_standard_version(str(run["base_version_id"]))
                if run["source_mode"] == "standard" else None)
            try:
                graph = self._analysis_for_run(run, job)
            except UsageDeferred:
                raise
            except Exception as exc:
                from ..inference.usage import current_scope
                if current_scope() is not None:
                    _raise_if_runpod_reconciliation(exc)
                assert_owned()
                failed = self.service.complete_run(
                    run_id,
                    "failed",
                    error=_safe_error_code(exc),
                    run_attempt=run_attempt,
                    owner_token=owner_token,
                )
                return {"run_id": run_id, "status": failed["status"]}

            for item in run["items"]:
                if item["status"] != "pending":
                    continue
                kind = VariantKind(str(item["variant_kind"]))
                try:
                    assert_permitted()
                    artifact_id = (self._build_career_variant(run, job, graph, kind, assert_permitted)
                        if run["source_mode"] == "career_profile" else
                        self._build_variant(run, job, graph, version, kind, assert_permitted))
                    assert_owned()
                    self._while_application_preparing(
                        str(run["application_id"]),
                        job,
                        lambda _connection: self.service.complete_run_item(
                            run_id,
                            kind,
                            "succeeded",
                            artifact_id=artifact_id,
                            run_attempt=run_attempt,
                            owner_token=owner_token,
                        ),
                    )
                except _LostLease:
                    raise
                except _ApplicationLedgerUnavailable:
                    raise
                except _ApplicationStateConflict:
                    assert_owned()
                    failed = self.service.complete_run(
                        run_id,
                        "failed",
                        error="application_not_preparing",
                        run_attempt=run_attempt,
                        owner_token=owner_token,
                    )
                    return {"run_id": run_id, "status": failed["status"]}
                except UsageDeferred:
                    raise
                except Exception as exc:
                    from ..inference.usage import current_scope
                    if current_scope() is not None:
                        _raise_if_runpod_reconciliation(exc)
                    assert_owned()
                    self.service.complete_run_item(
                        run_id,
                        kind,
                        "failed",
                        error=_safe_error_code(exc),
                        run_attempt=run_attempt,
                        owner_token=owner_token,
                    )

            # Derive the terminal state from persisted items, including results from
            # before a process crash.  Invocation-local counters can wedge a run.
            assert_owned()
            persisted = self.service.get_run(run_id)
            completed = sum(
                item["status"] == "succeeded" for item in persisted["items"]
            )
            failures = sum(
                item["status"] == "failed" for item in persisted["items"]
            )
            if failures:
                reconciliation_error = next(
                    (
                        str(item.get("error") or "")
                        for item in persisted["items"]
                        if requires_runpod_reconciliation(item.get("error"))
                    ),
                    "",
                )
                final = self.service.complete_run(
                    run_id,
                    "failed",
                    error=reconciliation_error or "variant_generation_failed",
                    run_attempt=run_attempt,
                    owner_token=owner_token,
                )
            elif completed == len(persisted["items"]):
                try:
                    final = self._while_application_preparing(
                        str(run["application_id"]),
                        job,
                        lambda _connection: self.service.complete_run(
                            run_id,
                            "succeeded",
                            run_attempt=run_attempt,
                            owner_token=owner_token,
                        ),
                    )
                except _ApplicationStateConflict:
                    assert_owned()
                    final = self.service.complete_run(
                        run_id,
                        "failed",
                        error="application_not_preparing",
                        run_attempt=run_attempt,
                        owner_token=owner_token,
                    )
            else:  # pragma: no cover - loop/transaction invariant
                final = self.service.complete_run(
                    run_id,
                    "failed",
                    error="variant_generation_incomplete",
                    run_attempt=run_attempt,
                    owner_token=owner_token,
                )
            return {
                "run_id": run_id,
                "status": final["status"],
                "completed": completed,
                "failed": failures,
            }


def resume_lab_status(config: Any) -> Mapping[str, Any]:
    """Return redacted setup health without compiling, parsing, or invoking a model."""

    configured = bool(config.resume_lab_db and config.resume_artifact_root)
    local_toolchain_configured = bool(
        config.resume_tectonic_executable
        and config.resume_tectonic_bundle
        and config.resume_tectonic_version
    )
    tool_socket = getattr(config, "tool_service_socket", None)
    remote_toolchain_configured = bool(tool_socket and config.resume_tectonic_version)
    tool_service_ready = False
    remote_pypdf_ready = False
    if remote_toolchain_configured:
        try:
            from job_search.tool_service import SERVICE_REVISION, ToolServiceClient

            report = ToolServiceClient(tool_socket, timeout_seconds=3).call("ping", {})
            tool_service_ready = bool(
                report.get("service_revision") == SERVICE_REVISION
                and report.get("engine") == "tectonic"
                and report.get("engine_version") == config.resume_tectonic_version
                and re.fullmatch(r"[0-9a-f]{64}", str(report.get("bundle_sha256") or ""))
            )
            remote_pypdf_ready = bool(
                tool_service_ready
                and isinstance(report.get("pypdf_version"), str)
                and report.get("pypdf_version") != "unavailable"
            )
        except Exception:
            # Doctor reports one stable setup state and never leaks socket, parser,
            # or toolchain errors from the private service.
            tool_service_ready = False
            remote_pypdf_ready = False
    tectonic_configured = local_toolchain_configured or remote_toolchain_configured
    local_tectonic_ready = bool(
        local_toolchain_configured
        and Path(config.resume_tectonic_executable).is_file()
        and os.access(config.resume_tectonic_executable, os.X_OK)
        and Path(config.resume_tectonic_bundle).is_file()
    )
    tectonic_ready = tool_service_ready if remote_toolchain_configured else local_tectonic_ready
    pypdf_ready = (
        remote_pypdf_ready
        if remote_toolchain_configured
        else importlib.util.find_spec("pypdf") is not None
    )
    model_report: Mapping[str, Any]
    if config.resume_model_config is None:
        from .model import resume_model_status

        model_report = resume_model_status(None)
    else:
        try:
            from .model import load_resume_model_config, resume_model_status

            model_report = resume_model_status(
                load_resume_model_config(config.resume_model_config)
            )
        except Exception:
            model_report = {
                "status": "blocked_setup",
                "reason": "model_config_invalid",
                "command_available": False,
                "isolation_available": False,
            }
    standard_ready = False
    if configured and Path(config.resume_lab_db).is_file():
        try:
            with sqlite3.connect(Path(config.resume_lab_db).resolve().as_uri()+"?mode=ro", uri=True) as con:
                rows=con.execute("SELECT v.import_metadata_json FROM resume_standards s JOIN resume_standard_versions v ON s.active_version_id=v.version_id WHERE s.active=1").fetchall()
            import json
            root=Path(config.resume_artifact_root).resolve()
            for row in rows:
                meta=json.loads(row[0] or '{}')
                path=root / str(meta.get('managed_relative_path',''))
                if (meta.get('parse_safe') and root in path.resolve().parents and path.is_file()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == meta.get('pdf_sha256')):
                    standard_ready=True
        except (OSError,ValueError,sqlite3.Error):
            pass
    import_ready = configured and tectonic_ready and pypdf_ready
    model_configuration_ready = model_report.get("status") in {
        "ready",
        "configuration_ready",
    }
    generation_ready = import_ready and model_configuration_ready
    if not configured:
        reason = "resume_lab_not_configured"
    elif remote_toolchain_configured and not tool_service_ready:
        reason = "document_tool_service_unavailable"
    elif not tectonic_ready:
        reason = "tectonic_not_ready"
    elif not pypdf_ready:
        reason = "pypdf_not_installed"
    elif not model_configuration_ready:
        reason = str(model_report.get("reason") or "model_not_ready")
    else:
        reason = None
    status = "blocked_setup"
    if generation_ready:
        status = (
            "configuration_ready"
            if model_report.get("status") == "configuration_ready"
            else "ready"
        )
    if getattr(config, "resume_mode", "tailored") == "standard" and standard_ready:
        status, reason = "ready", None
    return {
        "status": status,
        "reason": reason,
        "standard_ready": standard_ready,
        "ready_for_existing_import": configured and pypdf_ready,
        "tailoring_status": "ready" if generation_ready else "disabled" if config.resume_model_config is None else "blocked",
        "configured": configured,
        "database_configured": config.resume_lab_db is not None,
        "artifact_repository_configured": config.resume_artifact_root is not None,
        "tectonic_configured": tectonic_configured,
        "tectonic_ready": tectonic_ready,
        "pypdf_ready": pypdf_ready,
        "tool_service_configured": remote_toolchain_configured,
        "tool_service_ready": tool_service_ready,
        "model": dict(model_report),
        "ready_for_import": import_ready,
        "ready_for_generation": generation_ready,
        "gateway_revision": GATEWAY_REVISION,
    }


def build_resume_lab_gateway(
    config: Any, *, allow_unmanaged_remote_inference: bool = False,
) -> Optional[ResumeLabProductionGateway]:
    """Build the optional production gateway from owner-only runtime configuration."""

    if config.resume_lab_db is None or config.resume_artifact_root is None:
        return None
    model = None
    if config.resume_model_config is not None:
        from .model import load_resume_model_config

        model = load_resume_model_config(config.resume_model_config).build()
    toolchain = None
    tool_socket = getattr(config, "tool_service_socket", None)
    if tool_socket:
        from job_search.tool_service import (
            RemotePypdfExtractor,
            RemoteTectonicCompiler,
        )
        from .toolchain import ResumeArtifactToolchain

        toolchain = ResumeArtifactToolchain(
            RemoteTectonicCompiler(tool_socket, config.resume_tectonic_version),
            RemotePypdfExtractor(tool_socket),
        )
    elif config.resume_tectonic_executable and config.resume_tectonic_bundle:
        from .pdf import PypdfExtractor
        from .tex import TectonicCompiler
        from .toolchain import ResumeArtifactToolchain

        toolchain = ResumeArtifactToolchain(
            TectonicCompiler(
                config.resume_tectonic_executable,
                config.resume_tectonic_bundle,
                config.resume_tectonic_version,
            ),
            PypdfExtractor(),
        )
    gateway = ResumeLabProductionGateway(
        ResumeLabService(config.resume_lab_db),
        ResumePdfArtifactRepository(config.resume_artifact_root),
        application_db=config.application_db,
        model=model,
        toolchain=toolchain,
        allow_unmanaged_remote_inference=allow_unmanaged_remote_inference,
        application_gateway=_configured_application_gateway(config),
    )
    if tool_socket:
        from job_search.tool_service import RemoteAttachmentExtractor
        gateway.career_attachment_extractor = RemoteAttachmentExtractor(tool_socket)
    return gateway


def build_resume_lab_read_gateway(
    config: Any,
) -> Optional[ResumeLabProductionGateway]:
    """Build the cached read view used across the Hermes trust boundary.

    Hermes can compare and inspect already-produced artifacts, but it cannot trigger a
    model request or TeX/PDF tool invocation.  Keeping those dependencies absent also
    means the MCP process never needs their credentials or sockets mounted.
    """

    if config.resume_lab_db is None or config.resume_artifact_root is None:
        return None
    return ResumeLabProductionGateway(
        ResumeLabService(config.resume_lab_db),
        ResumePdfArtifactRepository(config.resume_artifact_root),
        application_db=config.application_db,
        model=None,
        toolchain=None,
        application_gateway=_configured_application_gateway(config),
    )


def _configured_application_gateway(config):
    if getattr(config, "application_backend", "legacy") == "legacy":
        return None
    from ..application_gateway import build_application_gateway
    from ..service import JobSearchLedger
    return build_application_gateway(config, JobSearchLedger(config.application_db))


__all__ = [
    "GATEWAY_REVISION",
    "RESUME_OPTIMIZE_TASK",
    "ResumeLabProductionGateway",
    "ResumeSetupError",
    "build_resume_lab_gateway",
    "build_resume_lab_read_gateway",
    "enqueue_resume_run",
    "resume_lab_status",
]
