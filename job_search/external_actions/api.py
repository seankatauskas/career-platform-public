"""Public, exact-text contracts for external effects.

Import this module for contracts and the facade; provider code never decides what
an application result means.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ActionEnvelope:
    kind: str
    account_id: str
    application_id: str
    pursuit_no: int
    target: Mapping[str, Any]
    payload: Mapping[str, Any]
    context_versions: Mapping[str, int]
    consequence: Mapping[str, Any] | None = None
    allow_closed: bool = False


@dataclass(frozen=True)
class ProviderOutcome:
    """A trusted adapter's observation, never a model-supplied result.

    progress: confirmed intermediate checkpoint (for example, a verified draft).
    succeeded: exact final effect verified. accepted: effect awaiting observation.
    uncertain/mismatch: retain actual observations, no blind retry.
    not_executed: reconciliation has positively proved no effect occurred.
    """

    status: str
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    observation: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""


class ActionProvider(Protocol):
    """Every method performs I/O outside command transactions.

    Implementations must be bound to an authenticated account; preflight validates
    that binding, source identity/version, recipients or calendar ownership.
    """

    def preflight(self, action: Mapping[str, Any]) -> None: ...
    def perform(self, action: Mapping[str, Any]) -> ProviderOutcome: ...
    def reconcile(self, action: Mapping[str, Any]) -> ProviderOutcome: ...


class PreEffectTransientError(Exception):
    """Provider has positively established no write was attempted."""


from .service import ExternalActionOperations, SCHEMA, SCHEMA_MIGRATIONS  # noqa: E402

__all__ = ["ActionEnvelope", "ActionProvider", "ProviderOutcome",
           "PreEffectTransientError", "ExternalActionOperations", "SCHEMA", "SCHEMA_MIGRATIONS"]
