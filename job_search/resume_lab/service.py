"""Public deterministic facade for resume-lab state and scoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from .contracts import (
    ArtifactInput,
    ComparisonManifest,
    ComparisonSlot,
    JobSnapshot,
    RequirementGraph,
    ResumeBoundaryError,
    ResumeConflictError,
    ResumeLabError,
    ResumeNotFoundError,
    ResumePurpose,
    StandardVersionInput,
    VARIANT_PURPOSE,
    VariantKind,
    canonical_json,
)
from .requirements import extract_requirement_graph, requirement_graph_from_clauses
from .scoring import score_ats_proxy
from .store import ResumeLabStore


class ResumeLabService:
    """Own all resume-lab mutations without application-ledger authority."""

    def __init__(self, db_path: Path) -> None:
        self.store = ResumeLabStore(db_path)

    def create_standard(
        self,
        name: str,
        manual_rank: int,
        version: StandardVersionInput,
        *,
        actor_kind: str,
        active: bool = True,
    ) -> Mapping[str, Any]:
        if actor_kind != "user":
            raise ResumeBoundaryError("hand-written standards require a user actor")
        # Create inactive so an invalid version cannot occupy an active rank.
        standard = self.store.create_standard(
            name, manual_rank, actor_kind=actor_kind, active=False
        )
        saved_version = self.store.add_standard_version(
            str(standard["standard_id"]),
            version,
            actor_kind=actor_kind,
            activate=True,
        )
        if active:
            standard = self.store.set_standard_active(
                str(standard["standard_id"]), True, actor_kind=actor_kind
            )
        else:
            standard = self.store.get_standard(str(standard["standard_id"]))
        return {"standard": standard, "version": saved_version}

    def add_standard_version(
        self,
        standard_id: str,
        version: StandardVersionInput,
        *,
        actor_kind: str,
        activate: bool = True,
    ) -> Mapping[str, Any]:
        if actor_kind != "user":
            raise ResumeBoundaryError(
                "hand-written standard versions require a user actor"
            )
        return self.store.add_standard_version(
            standard_id, version, actor_kind=actor_kind, activate=activate
        )

    def activate_standard_version(
        self, standard_id: str, version_id: str, *, actor_kind: str
    ) -> Mapping[str, Any]:
        return self.store.activate_standard_version(
            standard_id, version_id, actor_kind=actor_kind
        )

    def set_standard_rank(
        self, standard_id: str, manual_rank: int, *, actor_kind: str
    ) -> Mapping[str, Any]:
        return self.store.set_standard_rank(
            standard_id, manual_rank, actor_kind=actor_kind
        )

    def set_standard_active(
        self, standard_id: str, active: bool, *, actor_kind: str
    ) -> Mapping[str, Any]:
        return self.store.set_standard_active(
            standard_id, active, actor_kind=actor_kind
        )

    def list_active_standards(self) -> Sequence[Mapping[str, Any]]:
        return self.store.list_active_standards()

    def resolve_standard(
        self, standard_id: Optional[str] = None
    ) -> Tuple[Mapping[str, Any], str]:
        active = self.store.list_active_standards()
        if not active:
            raise ResumeNotFoundError("no active hand-written resume standard exists")
        if standard_id is None:
            return active[0], "highest_manual_rank"
        selected = next(
            (item for item in active if item["standard_id"] == standard_id), None
        )
        if selected is None:
            raise ResumeNotFoundError("selected standard is not active")
        return selected, "manual_override"

    def build_comparison_manifest(
        self, job: JobSnapshot, *, standard_id: Optional[str] = None
    ) -> ComparisonManifest:
        """Return the exact v1 comparison, without scoring standards to select one."""

        job.validate()
        standards = self.store.list_active_standards()
        selected, reason = self.resolve_standard(standard_id)
        slots = []
        for standard in standards:
            slots.append(
                ComparisonSlot(
                    slot_id="standard:" + str(standard["standard_id"]),
                    variant_kind=VariantKind.STANDARD,
                    purpose=ResumePurpose.REAL_APPLICATION,
                    standard_id=str(standard["standard_id"]),
                    base_version_id=str(standard["active_version_id"]),
                    label=str(standard["name"]),
                )
            )
        selected_standard_id = str(selected["standard_id"])
        selected_version_id = str(selected["active_version_id"])
        derived = (
            VariantKind.GROUNDED_REWRITE,
            VariantKind.STANDARD_EXAGGERATED,
            VariantKind.MARKET_IDEAL,
            VariantKind.KEYWORD_ADVERSARIAL,
        )
        for kind in derived:
            based_on_standard = kind in {
                VariantKind.GROUNDED_REWRITE,
                VariantKind.STANDARD_EXAGGERATED,
            }
            slots.append(
                ComparisonSlot(
                    slot_id=kind.value,
                    variant_kind=kind,
                    purpose=VARIANT_PURPOSE[kind],
                    standard_id=selected_standard_id if based_on_standard else None,
                    base_version_id=selected_version_id if based_on_standard else None,
                    label=kind.value.replace("_", " ").title(),
                )
            )
        return ComparisonManifest(
            job_fingerprint=job.fingerprint,
            selected_standard_id=selected_standard_id,
            selected_version_id=selected_version_id,
            selection_reason=reason,
            slots=tuple(slots),
        )

    def register_artifact(self, value: ArtifactInput) -> Mapping[str, Any]:
        return self.store.register_artifact(value)

    def get_artifact(
        self, artifact_id: str, *, include_content: bool = False
    ) -> Mapping[str, Any]:
        return self.store.get_artifact(artifact_id, include_content=include_content)

    def find_standard_artifact(
        self,
        base_version_id: str,
        job_fingerprint: str,
        requirement_graph_fingerprint: str,
    ) -> Optional[Mapping[str, Any]]:
        return self.store.find_standard_artifact(
            base_version_id, job_fingerprint, requirement_graph_fingerprint
        )

    def evaluate_artifact(
        self,
        artifact_id: str,
        job: JobSnapshot,
        *,
        semantic_overrides: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Mapping[str, Any]:
        job.validate()
        artifact = self.store.get_artifact(artifact_id, include_content=True)
        if artifact["job_fingerprint"] != job.fingerprint:
            raise ResumeConflictError("job snapshot changed after artifact generation")
        graph = extract_requirement_graph(job)
        result = score_ats_proxy(
            str(artifact["parsed_text"]),
            graph,
            semantic_overrides=semantic_overrides,
        )
        cached = self.store.get_cached_evaluation(result.cache_key)
        if cached is not None:
            self.store.put_evaluation(artifact_id, graph, result)
            return {**cached, "cached": True}
        saved = self.store.put_evaluation(artifact_id, graph, result)
        return {**saved, "cached": False}

    def select_for_application(
        self,
        application_id: str,
        artifact_id: str,
        idempotency_key: str,
        *,
        actor_kind: str,
    ) -> Mapping[str, Any]:
        return self.store.select_for_application(
            application_id,
            artifact_id,
            idempotency_key,
            actor_kind=actor_kind,
        )

    def current_application_selection(
        self, application_id: str
    ) -> Optional[Mapping[str, Any]]:
        return self.store.current_application_selection(application_id)

    def get_selection_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[Mapping[str, Any]]:
        return self.store.get_selection_by_idempotency_key(idempotency_key)

    def approve_run(
        self,
        run_id: str,
        grounded_artifact_id: str,
        idempotency_key: str,
        *,
        actor_kind: str,
    ) -> Mapping[str, Any]:
        return self.store.approve_run(
            run_id,
            grounded_artifact_id,
            idempotency_key,
            actor_kind=actor_kind,
        )

    def get_run_approval(self, run_id: str) -> Optional[Mapping[str, Any]]:
        return self.store.get_run_approval(run_id)

    def get_approval_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[Mapping[str, Any]]:
        return self.store.get_approval_by_idempotency_key(idempotency_key)

    def create_run(
        self,
        application_id: str,
        job: JobSnapshot,
        idempotency_key: str,
        *,
        standard_id: Optional[str] = None,
        expected_base_version_id: Optional[str] = None,
        ranked_standards: Sequence[Mapping[str, Any]] = (),
        requirement_graph: Optional[RequirementGraph] = None,
        requirement_clauses: Sequence[Mapping[str, Any]] = (),
        requirement_extraction: str = "deterministic_fallback",
    ) -> Mapping[str, Any]:
        # Snapshot the exact JSON representation that the store will persist before
        # reconstructing the graph.  Besides normalizing enum/string values, this
        # prevents a caller from mutating a nested clause between validation and the
        # database write.
        job_snapshot = JobSnapshot(**json.loads(canonical_json(job)))
        clause_snapshot = json.loads(canonical_json(requirement_clauses))
        if not isinstance(clause_snapshot, list):
            raise ResumeLabError("requirement clauses must be a sequence")
        frozen_clauses = tuple(clause_snapshot)
        reconstructed = requirement_graph_from_clauses(
            job_snapshot, frozen_clauses, include_fallback=True
        )

        if requirement_graph is not None:
            if not isinstance(requirement_graph, RequirementGraph):
                raise ResumeLabError("requirement graph is invalid")
            if requirement_graph.job_fingerprint != job_snapshot.fingerprint:
                raise ResumeConflictError("requirement graph belongs to a different job")
            # Compare the complete frozen graph, not only its claimed fingerprint: a
            # caller must not be able to pair a valid fingerprint with different
            # requirements, terms, source spans, or extraction origins.
            if requirement_graph != reconstructed:
                raise ResumeConflictError(
                    "requirement graph does not exactly reconstruct from its clauses"
                )
        graph = reconstructed
        previous = self.store.get_run_by_idempotency_key(idempotency_key)
        if previous is not None:
            analysis = previous.get("requirement_analysis") or {}
            persisted_rankings = previous.get("ranked_standards")
            rankings_match = (
                not ranked_standards
                or canonical_json(persisted_rankings) == canonical_json(ranked_standards)
            )
            if (
                str(previous["application_id"]) != application_id
                or canonical_json(previous["job_snapshot"])
                != canonical_json(job_snapshot)
                or (
                    standard_id is not None
                    and str(previous["selected_standard_id"]) != standard_id
                )
                or (
                    expected_base_version_id is not None
                    and str(previous["base_version_id"])
                    != expected_base_version_id
                )
                or analysis.get("requirement_graph_fingerprint") != graph.fingerprint
                or canonical_json(analysis.get("clauses") or ())
                != canonical_json(frozen_clauses)
                or analysis.get("extraction_source") != requirement_extraction
                or not rankings_match
            ):
                raise ResumeConflictError(
                    "run idempotency key belongs to different input"
                )
            return previous

        selected, _reason = self.resolve_standard(standard_id)
        base_version_id = str(
            expected_base_version_id or selected["active_version_id"]
        )
        if expected_base_version_id is not None:
            expected = self.store.get_standard_version(base_version_id)
            if str(expected["standard_id"]) != str(selected["standard_id"]):
                raise ResumeConflictError(
                    "expected base version does not belong to the selected standard"
                )
        return self.store.create_run(
            application_id,
            job_snapshot,
            str(selected["standard_id"]),
            base_version_id,
            idempotency_key,
            ranked_standards=ranked_standards,
            requirement_graph_fingerprint=graph.fingerprint,
            requirement_clauses=frozen_clauses,
            requirement_extraction=requirement_extraction,
        )

    def list_runs(
        self, statuses: Optional[Sequence[str]] = None, *, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_runs(statuses, limit=limit)

    def list_runs_for_job(
        self, ats: str, job_id: str, *, limit: int = 10
    ) -> Sequence[Mapping[str, Any]]:
        return self.store.list_runs_for_job(ats, job_id, limit=limit)

    def get_run(self, run_id: str) -> Mapping[str, Any]:
        return self.store.get_run(run_id)

    def get_run_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[Mapping[str, Any]]:
        return self.store.get_run_by_idempotency_key(idempotency_key)

    def get_latest_application_run(
        self, application_id: str
    ) -> Optional[Mapping[str, Any]]:
        return self.store.get_latest_application_run(application_id)

    def start_run(
        self, run_id: str, *, owner_token: Optional[str] = None
    ) -> Mapping[str, Any]:
        return self.store.start_run(run_id, owner_token=owner_token)

    def assert_run_owner(
        self, run_id: str, run_attempt: int, owner_token: str
    ) -> None:
        self.store.assert_run_owner(run_id, run_attempt, owner_token)

    def complete_run_item(
        self,
        run_id: str,
        variant_kind: VariantKind,
        outcome: str,
        *,
        artifact_id: Optional[str] = None,
        error: str = "",
        run_attempt: Optional[int] = None,
        owner_token: Optional[str] = None,
    ) -> Mapping[str, Any]:
        return self.store.complete_run_item(
            run_id,
            variant_kind,
            outcome,
            artifact_id=artifact_id,
            error=error,
            run_attempt=run_attempt,
            owner_token=owner_token,
        )

    def complete_run(
        self,
        run_id: str,
        outcome: str,
        *,
        error: str = "",
        run_attempt: Optional[int] = None,
        owner_token: Optional[str] = None,
    ) -> Mapping[str, Any]:
        return self.store.complete_run(
            run_id,
            outcome,
            error=error,
            run_attempt=run_attempt,
            owner_token=owner_token,
        )

    def fail_queued_run(self, run_id: str, *, error: str) -> Mapping[str, Any]:
        return self.store.fail_queued_run(run_id, error=error)

    def cancel_active_run_for_application_phase(
        self, run_id: str, *, error: str = "application_not_preparing"
    ) -> Mapping[str, Any]:
        return self.store.cancel_active_run_for_application_phase(
            run_id, error=error
        )

    def get_retry_command(
        self, idempotency_key: str
    ) -> Optional[Mapping[str, Any]]:
        return self.store.get_retry_command(idempotency_key)

    def retry_run(
        self,
        run_id: str,
        idempotency_key: str,
        *,
        actor_kind: str,
        reconciliation_acknowledged: bool = False,
    ) -> Mapping[str, Any]:
        return self.store.retry_run(
            run_id,
            idempotency_key,
            actor_kind=actor_kind,
            reconciliation_acknowledged=reconciliation_acknowledged,
        )


__all__ = ["ResumeLabService"]
