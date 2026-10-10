"""References shared across owners; possession of a reference grants no access."""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EvidenceRef:
    owner: str
    source_id: str
    revision: str
    sha256: str
    occurred_at: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    quote: Optional[str] = None
