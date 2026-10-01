#!/usr/bin/env python3
"""Run the same complete local fixture used for manual dashboard review."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from scripts.offline_system_demo import run


def test_complete_offline_system():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "state"
        args = SimpleNamespace(state_dir=root, serve=False, port=0, tectonic=None,
            bundle=None, tectonic_version=None)
        with contextlib.redirect_stdout(io.StringIO()):
            report = run(args)
        assert report == json.loads((root / "acceptance.json").read_text())
        assert report["status"] == "passed" and report["fixture"]
        assert len(report["checks"]) >= 13 and report["notifications_delivered"] >= 1
        with sqlite3.connect(root / "applications.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM work_items WHERE status!='succeeded'").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM outlook_message_stage WHERE processing_status='failed'").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM outbox_messages WHERE topic='recommendation.applied' AND status='delivered'").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM action_proposals WHERE status='executed'").fetchone()[0] == 1


if __name__ == "__main__":
    test_complete_offline_system()
    print("ok (1 full offline system scenario)")
