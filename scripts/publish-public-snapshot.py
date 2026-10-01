#!/usr/bin/env python3
"""Create one public snapshot commit per Chicago date; pushing is a separate step."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_export', ROOT / 'scripts/prepare-public-showcase.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def git(root: Path, *args: str, check: bool = True):
    return subprocess.run(['git', *args], cwd=root, check=check, capture_output=True, text=True)


def publish_snapshot(snapshot: Path, public: Path, day: str) -> dict:
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError('Use an ISO publication date')
    if snapshot.resolve() == public.resolve() or public.resolve() in snapshot.resolve().parents:
        raise ValueError('Snapshot must be outside the destination checkout')
    if git(public, 'symbolic-ref', '--short', 'HEAD').stdout.strip() != 'main':
        raise ValueError('Publish only to main')
    if git(public, 'status', '--porcelain', '--untracked-files=all').stdout:
        raise ValueError('Public checkout must be clean')
    head = git(public, 'rev-parse', '--verify', 'HEAD', check=False)
    initial = head.returncode != 0
    if not initial:
        message = git(public, 'log', '-1', '--format=%B').stdout
        prior = re.search(r'^Snapshot-Date: (\d{4}-\d{2}-\d{2})$', message, re.M)
        if not prior:
            raise ValueError('Public HEAD was not produced by the snapshot publisher')
        if prior[1] > day:
            raise ValueError('Publication date precedes the last snapshot')
        if prior[1] == day:
            return {'status': 'already_published', 'day': day, 'commit': head.stdout.strip()}
    sources = {}
    for source in snapshot.rglob('*'):
        if source.is_symlink():
            raise ValueError('Snapshot symlinks are not allowed')
        if source.is_file():
            name = source.relative_to(snapshot).as_posix()
            if '.git' in Path(name).parts or exporter.guard.private_path(name):
                raise ValueError('Private snapshot path: ' + name)
            if any(pattern.search(source.read_bytes()) for pattern in exporter.guard.SECRET_PATTERNS):
                raise ValueError('Possible credential: ' + name)
            sources[name] = source
    if not {'README.md', 'LICENSE'}.issubset(sources):
        raise ValueError('Snapshot must retain README and license attribution')
    tracked = git(public, 'ls-files', '-z').stdout.split('\0')
    for name in filter(None, tracked):
        path = public / name
        if '.git' in Path(name).parts or path.is_symlink():
            raise ValueError('Unexpected destination path')
        if name not in sources:
            path.unlink()
            parent = path.parent
            while parent != public:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    for name, source in sources.items():
        target = public / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(source.stat().st_mode & 0o777)
    git(public, 'add', '--update')
    # The source intentionally tracks reviewed JSON despite broad runtime ignores.
    # Force only the reviewed export paths, never ignored local runtime artifacts.
    git(public, 'add', '--force', '--', *sorted(sources))
    if not initial and git(public, 'diff', '--cached', '--quiet', check=False).returncode == 0:
        return {'status': 'unchanged', 'day': day, 'commit': head.stdout.strip()}
    title = 'Initial public release' if initial else 'Daily update — ' + day
    git(public, 'commit', '-m', title, '-m', 'Snapshot-Date: ' + day)
    return {'status': 'committed', 'day': day,
            'commit': git(public, 'rev-parse', 'HEAD').stdout.strip(), 'files': len(sources)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-checkout', type=Path, required=True)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--day', default=datetime.now(ZoneInfo('America/Chicago')).date().isoformat())
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='career-public-export-') as temporary:
        snapshot = Path(temporary) / 'snapshot'
        report = exporter.export_tree(ROOT, args.revision, snapshot)
        result = publish_snapshot(snapshot, args.public_checkout.resolve(), args.day)
        print(json.dumps({**result, 'source_sha': report['source_sha']}))


if __name__ == '__main__':
    main()
