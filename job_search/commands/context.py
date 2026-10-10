"""Authority is constructed by trusted ingress, never decoded from request bodies."""
from dataclasses import dataclass, field
from typing import FrozenSet, Optional


class DomainError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Principal:
    actor_id: str
    kind: str
    capabilities: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self):
        if not self.actor_id or self.kind not in {"human", "agent", "worker"}:
            raise DomainError("not_authorized", "Invalid trusted principal")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


@dataclass(frozen=True)
class Delegation:
    """An exact grant supplied by trusted composition, not an agent tool argument."""
    issuer_id: str
    recipient_id: str
    operation: str
    payload_digest: str
    expires_at: str


@dataclass(frozen=True)
class CommandContext:
    principal: Principal
    idempotency_key: str
    origin: str = "direct"
    delegation: Optional[Delegation] = None
    causation_id: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.idempotency_key, str) or not 1 <= len(self.idempotency_key) <= 256:
            raise DomainError("invalid_input", "A bounded command identity is required")
        if self.origin not in {"direct", "inferred", "scheduled", "result", "migration"}:
            raise DomainError("invalid_input", "Invalid command origin")
