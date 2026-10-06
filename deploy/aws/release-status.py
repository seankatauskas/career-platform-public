#!/usr/bin/env python3
"""Read-only release facts, embedded in a parameterless SSM document by Terraform.

No application imports, shell execution, AWS calls, or personal state output.
Existing locks are inspected without creating or modifying files.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat


def inspect(release_root=Path('/opt/job-search'), data_root=Path('/var/lib/job-search')):
    result = {'version': 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
              'state': 'unknown', 'installed': None, 'operation': None}
    try:
        release_root, data_root = release_root.resolve(strict=True), data_root.resolve(strict=True)
        if not release_root.is_dir() or not data_root.is_dir():
            raise ValueError('missing host directories')
        with ExitStack() as held:
            # Retain both shared locks throughout the read. Writers use EX locks.
            for path in (release_root / '.install.lock', data_root / '.operations.lock'):
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except FileNotFoundError:
                    continue
                held.callback(os.close, fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError('invalid lock')
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    result['state'] = 'maintenance'
                    return result
            journal = data_root / 'operations/current.json'
            if journal.exists() or journal.is_symlink():
                if journal.is_symlink():
                    raise ValueError('invalid journal')
                value = json.loads(journal.read_text())
                if (value.get('version') != 1 or not isinstance(value.get('complete'), bool)
                        or not re.fullmatch(r'[a-f0-9]{32}', value.get('operation_id', ''))
                        or value.get('kind') not in {'deploy', 'rollback', 'restore', 'backup'}):
                    raise ValueError('invalid journal')
                result['operation'] = {key: value[key] for key in ('operation_id', 'kind', 'complete')}
                if not value['complete']:
                    result['state'] = 'recovery_required'
                    return result
            current = release_root / 'current'
            if current.exists() or current.is_symlink():
                target = current.resolve(strict=True)
                if target.parent != release_root / 'releases':
                    raise ValueError('invalid release path')
                value = json.loads((target / 'release.json').read_text())
                source, release = value.get('source_sha', ''), value.get('release_id', '')
                if (not re.fullmatch(r'[a-f0-9]{40}', source)
                        or not re.fullmatch(re.escape(source) + r'-[0-9]+', release)
                        or target.name != release or value.get('operations_protocol') != 1):
                    raise ValueError('invalid installed release')
                result['installed'] = {'release_id': release, 'source_sha': source}
            elif result['operation'] is not None:
                raise ValueError('journal exists without installed release')
            result['state'] = 'idle'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result.update(state='unknown', installed=None)
    return result


if __name__ == '__main__':
    print(json.dumps(inspect()))
