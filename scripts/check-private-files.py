#!/usr/bin/env python3
"""Reject private runtime artifacts from the index (also used before publication)."""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = {"job_search/resume_lab/jake_template.tex", "job_search/resume_lab/career_ops_template.tex"}
PRIVATE_PARTS = {"private", "secrets", "backups", "resume-artifacts", ".cache", ".models", ".venv"}
PRIVATE_SUFFIXES = (".pdf", ".tex", ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm", ".tar", ".tar.gz", ".zip", ".bak", ".pem", ".key", ".token", ".secret", ".tfstate", ".tfvars", ".p12", ".pfx")
SECRET_PATTERNS = [re.compile(rb"(?:AKIA|ASIA)[0-9A-Z]{16}"), re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"), re.compile(rb"github_pat_[A-Za-z0-9_]{40,}"), re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), re.compile(rb"sk-proj-[A-Za-z0-9_-]{40,}"), re.compile(rb"sk-or-v1-[A-Za-z0-9_-]{40,}")]

def private_path(name: str) -> bool:
    path = Path(name)
    if name in TEMPLATES:
        return False
    return (bool(set(path.parts) & PRIVATE_PARTS)
            or name.lower().endswith(PRIVATE_SUFFIXES)
            or path.name.startswith(("resume-content", "resume-provenance", "Sean_Katauskas_Resume"))
            or path.name in {"mcp-token", "portable-master-key", "runpod-api-key", "openrouter-api-key"}
            or (path.name.startswith(".env") and not path.name.endswith(".example")))

def inspect_index(root: Path = ROOT, revision: str | None = None) -> list[str]:
    # Inspect staged bytes, not working-tree bytes: a deletion or partial staging
    # must not hide sensitive content about to be committed.
    command = ["git", "ls-tree", "-rz", "--name-only", revision] if revision else ["git", "ls-files", "-z"]
    names = subprocess.check_output(command, cwd=root).decode().split("\0")
    failures = []
    for name in filter(None, names):
        if private_path(name):
            failures.append(name + ": private artifact")
            continue
        if Path(name).suffix in {".png", ".mp4"}:
            continue  # Reviewed fictional demo media only.
        raw = subprocess.check_output(["git", "show", (revision or "") + ":" + name], cwd=root)
        if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
            failures.append(name + ": possible credential")
    return failures

def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    failures = inspect_index()
    for message in failures:
        print(message)
    print("private-file check: " + ("FAILED" if failures else "passed"))
    return int(bool(failures))

if __name__ == "__main__":
    raise SystemExit(main())
