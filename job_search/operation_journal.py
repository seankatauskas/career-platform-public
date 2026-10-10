"""Host-only durable maintenance intent; survives process death and volume restore."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


class OpsError(RuntimeError):
    pass


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(path: Path) -> None:
    for child in path.rglob("*"):
        if child.is_file():
            with child.open("rb") as stream:
                os.fsync(stream.fileno())
    for child in sorted((p for p in path.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        sync_directory(child)
    sync_directory(path)


def write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise OpsError("operation path is a symlink")
    fd, name = tempfile.mkstemp(prefix=".ops-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read(c: dict) -> dict | None:
    path = Path(c["data_root"]) / "operations" / "current.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if (value.get("version") != 1 or
                not re.fullmatch(r"[a-f0-9]{32}", value.get("operation_id", "")) or
                value.get("kind") not in {"deploy", "rollback", "restore", "backup"} or
                not isinstance(value.get("complete"), bool)):
            raise ValueError()
        return value
    except (ValueError, TypeError, OSError):
        raise OpsError("operation journal is unreadable; preserve state for inspection") from None


def require_idle(c: dict) -> None:
    value = read(c)
    if value and not value["complete"]:
        raise OpsError("recovery required for operation " + value["operation_id"])


def set_gate(c: dict, allowed: list[str], *, draining: bool = False, initialize: str | None = None) -> None:
    root = Path(c["data_root"]) / "maintenance"
    if root.is_symlink():
        raise OpsError("maintenance directory is a symlink")
    root.mkdir(mode=0o755, exist_ok=True)
    root.chmod(0o755)
    write_json(root / "gate.json", {"version": 1, "allowed_services": allowed,
               "draining": draining, "initialize_operation": initialize}, mode=0o644)


class Operation:
    def __init__(self, c: dict, value: dict):
        self.c, self.value = c, value
        self.root = Path(c["data_root"]) / "operations"

    @classmethod
    def begin(cls, c: dict, kind: str, **fields: Any) -> "Operation":
        require_idle(c)
        root = Path(c["data_root"]) / "operations"
        if root.is_symlink():
            raise OpsError("operations directory is a symlink")
        root.mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        sync_directory(root.parent)
        previous = read(c)
        if previous:
            write_json(root / (previous["operation_id"] + ".json"), previous)
        op = cls(c, {"version": 1, "operation_id": os.urandom(16).hex(), "kind": kind,
                     "phase": "prepared", "complete": False, "writes_possible": False,
                     "started_at": now(), "phase_times": {}, **fields})
        op.update("prepared")
        return op

    def update(self, phase: str, **fields: Any) -> None:
        self.value.update(fields, phase=phase, updated_at=now())
        self.value["phase_times"].setdefault(phase, self.value["updated_at"])
        write_json(self.root / "current.json", self.value)

    def finish(self, phase: str, **fields: Any) -> None:
        self.update(phase, complete=True, **fields)

    def durations(self) -> dict:
        """Completed intervals, without treating worker drain as UI downtime."""
        phases = self.value["phase_times"]
        pairs = {"drain": (phases.get("draining"), phases.get("stopping")),
                 "stop": (phases.get("stopping"), phases.get("quiesced")),
                 "snapshot": (phases.get("snapshotting"), phases.get("snapshotted")),
                 "runtime_preparation": (phases.get("preparing_runtime"), phases.get("initializing")),
                 "initialize": (phases.get("initializing"), phases.get("validating")),
                 "validation": (phases.get("validating"), phases.get("resuming")),
                 "service_startup": (phases.get("resuming"), self.value.get("downtime_finished_at")),
                 "maintenance_window": (self.value.get("downtime_started_at"), self.value.get("downtime_finished_at"))}
        result = {name: round(max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()), 3)
                  for name, (start, end) in pairs.items() if start and end}
        if "maintenance_window" in result:
            result["downtime"] = result["maintenance_window"] if self.value.get("active_services") else 0.0
        return result

    @property
    def id(self) -> str:
        return self.value["operation_id"]
