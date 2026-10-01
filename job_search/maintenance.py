"""Read-only cloud startup/drain gate. Contains no credentials or host authority."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def gate() -> dict | None:
    path = os.environ.get("JOB_SEARCH_MAINTENANCE_GATE")
    if not path:
        return None  # Local launchd and ordinary CLI use have no cloud gate.
    value = json.loads(Path(path).read_text())
    if value.get("version") != 1 or not isinstance(value.get("allowed_services"), list):
        raise RuntimeError("invalid maintenance gate")
    return value


def require_start(service: str) -> None:
    value = gate()
    if value is None:
        return
    if service == "initialize":
        operation = os.environ.get("JOB_SEARCH_INITIALIZE_OPERATION")
        if operation and operation == value.get("initialize_operation"):
            return
    elif service in value["allowed_services"]:
        return
    raise RuntimeError("maintenance blocks startup; inspect host operations status")


def draining() -> bool:
    try:
        value = gate()
        return bool(value and value.get("draining"))
    except (OSError, ValueError, RuntimeError):
        return True  # Missing/corrupt cloud gate must never claim fresh work.


def main() -> int:
    try:
        service, *command = sys.argv[1:]
        require_start(service)
        if command:
            os.execvp(command[0], command)
        return 0
    except (OSError, ValueError, RuntimeError):
        print("maintenance blocks startup; inspect host operations status", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
