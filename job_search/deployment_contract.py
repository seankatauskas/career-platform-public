"""Source-bound compatibility identity, with a narrowly reviewed read-only scope.

Protocol 2 permits exact, audited versions of the diagnostics composition module to differ.
All writers, schemas, dependencies and unknown Python remain fingerprinted. The
review declaration is not evidence: both images must also pass diagnostics and
startup preservation probes and the candidate-write/predecessor-read roundtrip.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

PROTOCOL = 2
AUDIT_PATH = 'deploy/compatible-backend.json'
AUDITED_BACKEND = frozenset({'job_search/runtime_readiness.py'})


def covered(path: str) -> bool:
    if path.startswith('job_search/'):
        return not (path.startswith('job_search/web/') and PurePosixPath(path).suffix in {'.js', '.css', '.html'})
    return (path.startswith(('deploy/aws/', 'deploy/hermes/', 'requirements/')) or
            path.startswith('Dockerfile') or path.startswith('compose.') or
            path in {'.dockerignore', 'scripts/job-search-ops', 'scripts/job-search-seed'})


def reviewed_backend(files: dict[str, bytes], declaration: bytes | None) -> set[str]:
    """An absent, stale or malformed review never exempts a module's bytes.

    Digests cover raw source. Git executable modes are separately fingerprinted.
    Updating these declarations requires a review of persisted effects/callers,
    not a generated hash refresh simply to make a release eligible.
    """
    try:
        value = json.loads(declaration) if declaration is not None else None
        if (not isinstance(value, dict) or set(value) != {'protocol', 'modules'} or
                type(value['protocol']) is not int or value['protocol'] != PROTOCOL or
                not isinstance(value['modules'], dict) or
                set(value['modules']) != AUDITED_BACKEND):
            return set()
        if any(not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
               for digest in value['modules'].values()):
            return set()
        # Review the complete supported scope as one boundary. Stale or incomplete audits keep
        # every module covered, rather than partially approving a new release.
        if any(path not in files or hashlib.sha256(files[path]).hexdigest() != digest
               for path, digest in value['modules'].items()):
            return set()
        return set(AUDITED_BACKEND)
    except (ValueError, TypeError, UnicodeError):
        return set()


def fingerprint(files: dict[str, bytes], *, modes: dict[str, str] | None = None) -> str:
    reviewed = reviewed_backend(files, files.get(AUDIT_PATH))
    entries = {path: {'mode': (modes or {}).get(path, '100644'),
                     'content': 'reviewed-read-only-diagnostics-v2' if path in reviewed else hashlib.sha256(content).hexdigest()}
               for path, content in files.items() if covered(path)}
    if not {'job_search/aws_ops.py', 'compose.cloud.yaml'} <= entries.keys():
        raise ValueError('runtime contract is missing required source files')
    payload = {'protocol': PROTOCOL, 'files': entries}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def committed_inputs(repo: Path, source: str) -> tuple[dict[str, bytes], dict[str, str]]:
    """Read immutable Git blobs, never local configuration or generated state."""
    records = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-rz', '--full-tree', source])
    files, modes = {}, {}
    for record in records.split(b'\0'):
        if not record:
            continue
        header, name = record.split(b'\t', 1)
        path = name.decode('utf-8')
        if not covered(path) and path != AUDIT_PATH:
            continue
        mode, kind, oid = header.decode('ascii').split()
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ValueError('runtime contract refuses nonregular files')
        modes[path] = mode
        files[path] = subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'blob', oid])
    return files, modes


def committed_files(repo: Path, source: str) -> dict[str, bytes]:
    return committed_inputs(repo, source)[0]


def source_protocol(files: dict[str, bytes]) -> int:
    """Read the declared version without executing code from another revision."""
    if 'job_search/schema_lifecycle.py' not in files:
        return 0
    try:
        tree = ast.parse(files.get('job_search/deployment_contract.py', b''))
        values = [node.value.value for node in tree.body
                  if isinstance(node, ast.Assign) and len(node.targets) == 1 and
                  isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'PROTOCOL' and
                  isinstance(node.value, ast.Constant)]
        if len(values) == 1 and type(values[0]) is int:
            return values[0]
    except (SyntaxError, ValueError):
        pass
    # Protocol 1 predates the source capability declaration.
    return 1


def from_commit(repo: Path, source: str) -> str:
    files, modes = committed_inputs(repo, source)
    return fingerprint(files, modes=modes)


def protocol_from_commit(repo: Path, source: str) -> int:
    return source_protocol(committed_files(repo, source))


def audit_current_from_commit(repo: Path, source: str) -> bool:
    files = committed_files(repo, source)
    return reviewed_backend(files, files.get(AUDIT_PATH)) == AUDITED_BACKEND


def describe_commit(repo: Path, source: str) -> dict:
    """Derive all capabilities from one read of the same immutable source tree."""
    files, modes = committed_inputs(repo, source)
    return {'protocol': source_protocol(files), 'runtime_contract_sha256': fingerprint(files, modes=modes),
            'backend_audit_current': reviewed_backend(files, files.get(AUDIT_PATH)) == AUDITED_BACKEND}
