"""Narrow product-facing port for the optional resume laboratory.

The resume core owns persistence, scoring, generation, and artifact storage.  The
dashboard and Hermes know only this bounded interface: no database handle, model,
subprocess, or filesystem path crosses it.  Keeping the port mapping-based lets the
core and toolchain evolve independently while their public records are being joined.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


COMPARISON_KINDS = (
    "grounded_rewrite",
    "standard_exaggerated",
    "market_ideal",
    "keyword_adversarial",
)
MAX_STANDARD_RESUMES = 25


class ResumeLabUnavailable(RuntimeError):
    """The optional local resume laboratory has not been configured."""

    status = 503


@dataclass(frozen=True)
class ResumeArtifactContent:
    """Opaque artifact bytes safe for the dashboard to download.

    A gateway must resolve and read its own managed storage.  Returning bytes rather
    than a path prevents the HTTP layer or Hermes from gaining filesystem authority.
    """

    artifact_id: str
    filename: str
    content_type: str
    content: bytes
    sha256: str = ""


class ResumeLabGateway(Protocol):
    """High-level operations required by dashboard and read-only Hermes surfaces."""

    def use_standard(self, job: Mapping[str, Any], *, application_id: str, idempotency_key: str) -> Mapping[str, Any]: ...

    def prepare(
        self,
        job: Mapping[str, Any],
        *,
        application_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def list_standards(self, *, limit: int = MAX_STANDARD_RESUMES) -> Mapping[str, Any]: ...

    def start_run(
        self,
        job: Mapping[str, Any],
        *,
        application_id: str,
        standard_version_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def get_run_result(self, run_id: str) -> Mapping[str, Any]: ...

    def retry_run(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        reconciliation_acknowledged: bool = False,
    ) -> Mapping[str, Any]: ...

    def approve_run(
        self,
        run_id: str,
        *,
        comparison_kind: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def select_resume(
        self,
        application_id: str,
        *,
        job: Mapping[str, Any],
        artifact_id: str,
        evaluation_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def get_selection(self, application_id: str) -> Mapping[str, Any]: ...

    def get_application_resume_content(self, application_id: str) -> Mapping[str, Any]: ...

    def get_application_workspace(
        self, application_id: str
    ) -> Mapping[str, Any]: ...

    def match_uploaded_resume(self, digest: str) -> Mapping[str, Any] | None: ...

    def get_autofill_resume(self, application_id: str | None = None) -> ResumeArtifactContent | None: ...

    def get_standard_document(self, version_id: str, *, expected_sha256: str | None = None) -> ResumeArtifactContent: ...

    def get_artifact(self, artifact_id: str) -> ResumeArtifactContent: ...

    def get_artifact_source(self, artifact_id: str) -> ResumeArtifactContent: ...

    def get_career_profile(self) -> Mapping[str, Any]: ...

    def save_career_profile(
        self, content: Mapping[str, Any], *, expected_revision_id: str | None,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def approve_career_profile(
        self, revision_id: str, *, idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def export_career_profile(self) -> Mapping[str, Any]: ...

    def import_career_standard(
        self, standard_version_id: str, *, idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def import_career_document(
        self, content: bytes, *, filename: str, content_type: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def get_career_import(self, import_id: str) -> Mapping[str, Any]: ...

    def regenerate_career_run(
        self, run_id: str, *, pinned_fact_ids: Sequence[str],
        excluded_fact_ids: Sequence[str], idempotency_key: str,
        use_latest_profile: bool = False,
    ) -> Mapping[str, Any]: ...

    def start_research_comparisons(
        self, run_id: str, *, idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def compare_for_job(self, ats: str, job_id: str) -> Mapping[str, Any]: ...


class ExactJobCatalog(Protocol):
    """Read one normalized job without granting general database access."""

    def get_job(self, ats: str, job_id: str) -> Mapping[str, Any]: ...


__all__ = [
    "COMPARISON_KINDS",
    "ExactJobCatalog",
    "MAX_STANDARD_RESUMES",
    "ResumeArtifactContent",
    "ResumeLabGateway",
    "ResumeLabUnavailable",
]
