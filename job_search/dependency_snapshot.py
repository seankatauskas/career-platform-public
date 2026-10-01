"""Persist code-only core-worker diagnostics for processes without provider secrets."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from .readiness import STATUSES, _capability, _dependency_capabilities
from .scheduler import as_utc, utc_stamp


def revision() -> str:
    value = os.environ.get("JOB_SEARCH_SOURCE_REVISION", "")
    return value if re.fullmatch(r"[a-f0-9]{40}", value) else ""


def publish_snapshot(db_path: Path, dependencies: Mapping[str, Any], *, now: datetime | None = None) -> None:
    # Only fixed capability codes and booleans pass this boundary, never the raw
    # health object (which may contain paths, model configuration or error text).
    capabilities = _dependency_capabilities(dependencies)
    stamp = utc_stamp(as_utc(now or datetime.now(timezone.utc)))
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)) as con, con:
        con.execute("INSERT INTO runtime_dependency_snapshot(singleton,observed_at,source_revision,capabilities_json) "
                    "VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at, "
                    "source_revision=excluded.source_revision,capabilities_json=excluded.capabilities_json",
                    (stamp, revision(), json.dumps(capabilities, separators=(",", ":"))))


def read_snapshot(db_path: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = as_utc(now or datetime.now(timezone.utc))
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            row = con.execute("SELECT observed_at,source_revision,capabilities_json FROM runtime_dependency_snapshot WHERE singleton=1").fetchone()
        if row is None:
            raise ValueError("not observed")
        observed = as_utc(row[0])
        entries = json.loads(row[2])
        if not isinstance(entries, list) or len(entries) > 10:
            raise ValueError("invalid capabilities")
        output = []
        for entry in entries:
            if entry.get("id") not in {"inference", "resume", "notification_transport", "ranking_model"} or entry.get("status") not in STATUSES:
                raise ValueError("invalid capability")
            for key in ("reason_code", "next_action"):
                if not isinstance(entry.get(key), str) or not re.fullmatch(r"[a-z_]{1,80}", entry[key]):
                    raise ValueError("invalid code")
            output.append(_capability(entry["id"], entry["status"], configured=entry.get("configured"),
                enabled=entry.get("enabled"), attempt=utc_stamp(observed),
                reason=entry["reason_code"], action=entry["next_action"]))
        reason = ""
        if revision() and row[1] != revision():
            reason = "dependency_snapshot_release_changed"
        elif observed > current + timedelta(seconds=60) or current - observed > timedelta(minutes=15):
            reason = "dependency_snapshot_expired"
        if reason:
            # Do not retain old ready/disabled claims as current observations.
            return [_capability("dependency_snapshot", "stale", attempt=utc_stamp(observed),
                                reason=reason, action="inspect_core_worker")]
        return output
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        return [_capability("dependency_snapshot", "configured_unverified", reason="awaiting_core_observation", action="inspect_core_worker")]
