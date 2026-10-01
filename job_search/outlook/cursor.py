"""Storage-neutral optimistic cursor state for durable mail synchronization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Optional, Protocol, Tuple


class CursorConflict(RuntimeError):
    """Another worker advanced the same folder cursor."""


@dataclass(frozen=True)
class MailCursorState:
    account_id: str
    folder_ref: str
    query_version: int
    committed_delta_link: Optional[str] = None
    in_flight_next_link: Optional[str] = None
    revision: int = 0
    needs_backfill: bool = True


class CursorStateAdapter(Protocol):
    """Persistence seam implemented by the ledger database in the integrated app."""

    def load(self, account_id: str, folder_ref: str, query_version: int) -> MailCursorState: ...

    def checkpoint(self, state: MailCursorState, next_link: str) -> MailCursorState: ...

    def commit(self, state: MailCursorState, delta_link: str) -> MailCursorState: ...

    def reset(self, state: MailCursorState) -> MailCursorState: ...


class InMemoryCursorStateAdapter:
    """Reference optimistic-lock implementation and offline test fake."""

    def __init__(self) -> None:
        self._states: Dict[Tuple[str, str, int], MailCursorState] = {}

    @staticmethod
    def _key(state: MailCursorState) -> Tuple[str, str, int]:
        return (state.account_id, state.folder_ref, state.query_version)

    def load(self, account_id: str, folder_ref: str, query_version: int) -> MailCursorState:
        key = (account_id, folder_ref, query_version)
        return self._states.setdefault(
            key,
            MailCursorState(account_id, folder_ref, query_version),
        )

    def _replace(self, previous: MailCursorState, updated: MailCursorState) -> MailCursorState:
        key = self._key(previous)
        current = self._states.get(key)
        if current != previous:
            raise CursorConflict("mail cursor changed concurrently")
        self._states[key] = updated
        return updated

    def checkpoint(self, state: MailCursorState, next_link: str) -> MailCursorState:
        if not next_link:
            raise ValueError("next_link is required")
        return self._replace(
            state,
            replace(state, in_flight_next_link=next_link, revision=state.revision + 1),
        )

    def commit(self, state: MailCursorState, delta_link: str) -> MailCursorState:
        if not delta_link:
            raise ValueError("delta_link is required")
        return self._replace(
            state,
            replace(
                state,
                committed_delta_link=delta_link,
                in_flight_next_link=None,
                revision=state.revision + 1,
                needs_backfill=False,
            ),
        )

    def reset(self, state: MailCursorState) -> MailCursorState:
        return self._replace(
            state,
            replace(
                state,
                committed_delta_link=None,
                in_flight_next_link=None,
                revision=state.revision + 1,
                needs_backfill=True,
            ),
        )
