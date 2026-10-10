"""SQLite-backed Outlook sync state and privacy-minimized message staging."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from job_search.contracts import MailChange, utc_now
from job_search.db import connect, prepare_database

from .cursor import CursorConflict, MailCursorState


class SQLiteOutlookState:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        prepare_database(self.db_path, utc_now())

    @staticmethod
    def _state(row: sqlite3.Row) -> MailCursorState:
        return MailCursorState(
            account_id=str(row["account_id"]),
            folder_ref=str(row["folder_ref"]),
            query_version=int(row["query_version"]),
            committed_delta_link=row["committed_delta_link"],
            in_flight_next_link=row["in_flight_next_link"],
            revision=int(row["revision"]),
            needs_backfill=bool(row["needs_backfill"]),
        )

    def load(self, account_id: str, folder_ref: str, query_version: int) -> MailCursorState:
        if not isinstance(query_version, int) or isinstance(query_version, bool) or query_version < 1:
            raise ValueError("query_version must be a positive integer")
        stamp = utc_now()
        with connect(self.db_path) as con:
            con.execute(
                "INSERT OR IGNORE INTO outlook_sync_cursors "
                "(account_id,folder_ref,query_version,updated_at) VALUES (?,?,?,?)",
                (account_id, folder_ref, query_version, stamp),
            )
            row = con.execute(
                "SELECT * FROM outlook_sync_cursors WHERE account_id=? AND folder_ref=? "
                "AND query_version=?",
                (account_id, folder_ref, query_version),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to create Outlook cursor")
        return self._state(row)

    def _replace(self, previous: MailCursorState, updated: MailCursorState) -> MailCursorState:
        stamp = utc_now()
        with connect(self.db_path) as con:
            cursor = con.execute(
                "UPDATE outlook_sync_cursors SET committed_delta_link=?,"
                "in_flight_next_link=?,revision=?,needs_backfill=?,updated_at=? "
                "WHERE account_id=? AND folder_ref=? AND query_version=? AND revision=?",
                (
                    updated.committed_delta_link,
                    updated.in_flight_next_link,
                    updated.revision,
                    int(updated.needs_backfill),
                    stamp,
                    previous.account_id,
                    previous.folder_ref,
                    previous.query_version,
                    previous.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise CursorConflict("mail cursor changed concurrently")
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

    def stage_changes(
        self,
        account_id: str,
        folder_ref: str,
        changes: Sequence[MailChange],
        query_version: int = 1,
    ) -> int:
        if not isinstance(query_version, int) or isinstance(query_version, bool) or query_version < 1:
            raise ValueError("query_version must be a positive integer")
        stamp = utc_now()
        with connect(self.db_path) as con:
            for change in changes:
                con.execute(
                    "INSERT INTO outlook_message_stage "
                    "(account_id,folder_ref,query_version,immutable_message_id,conversation_id,"
                    "internet_message_id,sender,subject,received_at,modified_at,web_link,"
                    "removed,processing_status,first_seen_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_id,folder_ref,query_version,immutable_message_id) "
                    "DO UPDATE SET "
                    "conversation_id=excluded.conversation_id,"
                    "internet_message_id=excluded.internet_message_id,"
                    "sender=excluded.sender,subject=excluded.subject,"
                    "received_at=excluded.received_at,modified_at=excluded.modified_at,"
                    "web_link=excluded.web_link,removed=excluded.removed,"
                    "processing_status=CASE WHEN excluded.removed=1 THEN 'ignored' "
                    "WHEN outlook_message_stage.removed=1 THEN 'pending' "
                    "WHEN excluded.modified_at IS NOT outlook_message_stage.modified_at "
                    "THEN 'pending' ELSE processing_status END,updated_at=excluded.updated_at",
                    (
                        account_id,
                        folder_ref,
                        query_version,
                        change.immutable_id,
                        change.conversation_id or "",
                        change.internet_message_id or "",
                        change.sender_address or "",
                        change.subject or "",
                        change.received_at,
                        change.modified_at,
                        change.web_link or "",
                        int(change.removed),
                        "ignored" if change.removed else "pending",
                        stamp,
                        stamp,
                    ),
                )
        return len(changes)

    def replace_folder_inventory(
        self,
        account_id: str,
        folders: Sequence[Any],
        *,
        excluded_roots: Mapping[str, str],
    ) -> Mapping[str, int]:
        """Replace one discovery snapshot without persisting private folder names."""

        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id is required")
        if len(folders) > 10_000:
            raise ValueError("mail folder inventory exceeds its bound")
        by_id = {}
        for folder in folders:
            folder_id = str(getattr(folder, "folder_id", ""))
            parent_id = str(getattr(folder, "parent_folder_id", ""))
            if not folder_id or len(folder_id) > 2048 or len(parent_id) > 2048:
                raise ValueError("mail folder inventory contains an invalid id")
            if folder_id in by_id and by_id[folder_id] != folder:
                raise ValueError("mail folder inventory contains conflicting ids")
            by_id[folder_id] = folder
        roots = {str(key): str(value) for key, value in excluded_roots.items()}
        if any(not key or value not in {"junk", "deleted"} for key, value in roots.items()):
            raise ValueError("excluded folder roots are invalid")

        def exclusion(folder_id: str) -> str:
            visited = set()
            current = folder_id
            while current:
                if current in roots:
                    return roots[current]
                if current in visited:
                    raise ValueError("mail folder parent cycle detected")
                visited.add(current)
                parent = by_id.get(current)
                current = str(getattr(parent, "parent_folder_id", "")) if parent else ""
            return ""

        stamp = utc_now()
        with connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            revision = int(
                con.execute(
                    "SELECT COALESCE(MAX(discovery_revision),0)+1 "
                    "FROM outlook_folder_inventory WHERE account_id=?",
                    (account_id,),
                ).fetchone()[0]
            )
            con.execute(
                "UPDATE outlook_folder_inventory SET active=0,updated_at=? WHERE account_id=?",
                (stamp, account_id),
            )
            excluded_count = 0
            for folder_id, folder in by_id.items():
                reason = exclusion(folder_id)
                excluded_count += int(bool(reason))
                con.execute(
                    "INSERT INTO outlook_folder_inventory "
                    "(account_id,folder_id,parent_folder_id,is_hidden,is_excluded,"
                    "exclusion_reason,active,discovery_revision,first_seen_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,1,?,?,?) ON CONFLICT(account_id,folder_id) DO UPDATE SET "
                    "parent_folder_id=excluded.parent_folder_id,is_hidden=excluded.is_hidden,"
                    "is_excluded=excluded.is_excluded,exclusion_reason=excluded.exclusion_reason,"
                    "active=1,discovery_revision=excluded.discovery_revision,updated_at=excluded.updated_at",
                    (
                        account_id,
                        folder_id,
                        str(getattr(folder, "parent_folder_id", "")),
                        int(bool(getattr(folder, "is_hidden", False))),
                        int(bool(reason)),
                        reason,
                        revision,
                        stamp,
                        stamp,
                    ),
                )
        return {
            "revision": revision,
            "folders": len(by_id),
            "excluded": excluded_count,
            "eligible": len(by_id) - excluded_count,
        }

    def eligible_folders(self, account_id: str) -> Sequence[str]:
        with connect(self.db_path) as con:
            return tuple(
                str(row[0])
                for row in con.execute(
                    "SELECT folder_id FROM outlook_folder_inventory WHERE account_id=? "
                    "AND active=1 AND is_excluded=0 ORDER BY folder_id",
                    (account_id,),
                )
            )

    def pending_messages(
        self, limit: int = 100, *, query_version: int = 1, received_since: str | None = None,
        account_id: str | None = None, folder_refs: Sequence[str] | None = None
    ) -> Sequence[Mapping[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("pending-message limit must be between 1 and 500")
        if not isinstance(query_version, int) or isinstance(query_version, bool) or query_version < 1:
            raise ValueError("query_version must be a positive integer")
        if account_id is not None and (not isinstance(account_id, str) or not account_id):
            raise ValueError("account_id must be nonempty")
        if folder_refs is not None and (not folder_refs or len(folder_refs)>100 or any(not isinstance(f,str) or not f for f in folder_refs)):
            raise ValueError("folders must be a bounded nonempty list")
        folder_sql = " AND folder_ref IN ("+",".join("?" for _ in folder_refs)+")" if folder_refs is not None else ""
        folder_params = tuple(folder_refs or ())
        if received_since:
            from ..contracts import parse_utc
            parse_utc(received_since)
        with connect(self.db_path) as con:
            if received_since:
                # Older metadata must not consume each small processing batch and
                # delay new recruiting messages while the initial cursor catches up.
                con.execute("UPDATE outlook_message_stage SET processing_status='ignored',updated_at=? "
                            "WHERE processing_status='pending' AND query_version=? "
                            "AND (? IS NULL OR account_id=?) "
                            "AND (julianday(received_at) IS NULL OR julianday(received_at)<julianday(?))"+folder_sql,
                            (utc_now(),query_version,account_id,account_id,received_since,*folder_params))
            return [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM outlook_message_stage WHERE removed=0 "
                    "AND processing_status='pending' AND query_version=? "
                    "AND (? IS NULL OR account_id=?) "+folder_sql+" "
                    "ORDER BY received_at,immutable_message_id "
                    "LIMIT ?",
                    (query_version, account_id, account_id, *folder_params,limit),
                )
            ]

    def mark_message(
        self,
        account_id: str,
        folder_ref: str,
        immutable_message_id: str,
        status: str,
        error: str = "",
        *,
        query_version: int = 1,
    ) -> None:
        if status not in {"ignored", "processed", "failed"}:
            raise ValueError("invalid message processing status")
        if not isinstance(query_version, int) or isinstance(query_version, bool) or query_version < 1:
            raise ValueError("query_version must be a positive integer")
        with connect(self.db_path) as con:
            cursor = con.execute(
                "UPDATE outlook_message_stage SET processing_status=?,last_error=?,"
                "updated_at=? WHERE account_id=? AND folder_ref=? "
                "AND query_version=? AND immutable_message_id=?",
                (
                    status,
                    str(error)[:500],
                    utc_now(),
                    account_id,
                    folder_ref,
                    query_version,
                    immutable_message_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("staged Outlook message was not found")

    def mark_revision(self, account_id, folder_ref, immutable_message_id, *, query_version,
                      modified_at, status="processed", error=""):
        """Acknowledge exactly the staged version read before a network request."""
        if status not in {"ignored", "processed", "failed"}:
            raise ValueError("invalid message processing status")
        with connect(self.db_path) as con:
            changed = con.execute("UPDATE outlook_message_stage SET processing_status=?,last_error=?,updated_at=? WHERE account_id=? AND folder_ref=? AND query_version=? AND immutable_message_id=? AND modified_at IS ? AND processing_status='pending'",
                (status, str(error)[:500], utc_now(), account_id, folder_ref, query_version, immutable_message_id, modified_at)).rowcount
        return changed == 1

    def set_health(
        self,
        connector_key: str,
        status: str,
        detail: str = "",
        *,
        success: bool = False,
        next_attempt_at: str | None = None,
    ) -> None:
        if status not in {"healthy", "degraded", "reauth_required", "failed", "disabled"}:
            raise ValueError("invalid connector health status")
        stamp = utc_now()
        with connect(self.db_path) as con:
            con.execute(
                "INSERT INTO connector_health "
                "(connector_key,status,detail,last_attempt_at,last_success_at,"
                "next_attempt_at,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(connector_key) DO UPDATE SET status=excluded.status,"
                "detail=excluded.detail,last_attempt_at=excluded.last_attempt_at,"
                "last_success_at=CASE WHEN excluded.last_success_at IS NOT NULL "
                "THEN excluded.last_success_at ELSE connector_health.last_success_at END,"
                "next_attempt_at=excluded.next_attempt_at,updated_at=excluded.updated_at",
                (
                    connector_key,
                    status,
                    str(detail)[:500],
                    stamp,
                    stamp if success else None,
                    next_attempt_at,
                    stamp,
                ),
            )

    def health(self) -> Sequence[Mapping[str, Any]]:
        with connect(self.db_path) as con:
            return [dict(row) for row in con.execute(
                "SELECT * FROM connector_health ORDER BY connector_key"
            )]
