#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography>=44,<47", "pypdf>=5,<7", "reportlab>=4,<5"]
# ///
"""Start a separate local development dashboard with fictional application data."""
from __future__ import annotations

import argparse
import errno
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8775)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    # A fresh directory in this checkout prevents a preview from migrating or
    # resetting real application state, including state from another worktree.
    base = ROOT / ".cache" / "development"
    base.mkdir(parents=True, exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix="session-", dir=base))
    print(f"Development preview: http://127.0.0.1:{args.port}", flush=True)
    print(f"Fictional data only. Session files: {state}", flush=True)
    print("Refresh for HTML/CSS/JS edits. Restart this command for Python edits. Ctrl-C stops the preview.", flush=True)
    from scripts.offline_system_demo import main as demo
    try:
        demo(["--interactive", "--scenario", "portfolio", "--state-dir", str(state), "--port", str(args.port)])
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            parser.error(f"port {args.port} is already in use; choose another with --port")
        raise


if __name__ == "__main__":
    main()
