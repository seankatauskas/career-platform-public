"""Deterministic job-search application core.

The package deliberately keeps agents, HTTP handlers, Outlook credentials, and local
models outside the authoritative state transition boundary.  Consumers should call a
service implementing :class:`job_search.contracts.JobSearchService`.
"""

from .contracts import (
    API_VERSION,
    EVENT_SCHEMA_VERSION,
    ActionKind,
    ActionProposalInput,
    ApplicationEventType,
    ApplicationPhase,
    EventProposalInput,
    JobSnapshot,
    MutationContext,
    RecommendationProvenance,
    TerminalOutcome,
    canonical_json,
    payload_sha256,
)

__all__ = [
    "API_VERSION",
    "EVENT_SCHEMA_VERSION",
    "ActionKind",
    "ActionProposalInput",
    "ApplicationEventType",
    "ApplicationPhase",
    "EventProposalInput",
    "JobSnapshot",
    "MutationContext",
    "RecommendationProvenance",
    "TerminalOutcome",
    "canonical_json",
    "payload_sha256",
]
