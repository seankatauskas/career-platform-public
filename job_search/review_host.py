"""Trusted AWS host installation and launching of isolated job reviews.

Only the coordinator uses this module. Worker containers never mount these paths.
Generated configuration is a reviewed projection, not a rewrite of app settings.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

from .operation_journal import OpsError, write_json

WORKER_LABEL = 'org.career-platform.review.worker=true'
UNIT = 'job-search-review.service'
TIMER = 'job-search-review.timer'


def application_projection(value, data_root, release):
    """Map only evidence paths from the container namespace into host state."""
    if not isinstance(value, dict) or value.get('version') != 1:
        raise OpsError('invalid application configuration for review projection')
    state = Path(data_root) / 'state'
    result = {'version': 1, 'project_root': str(release)}
    required = {'application_db', 'jobs_db', 'preference_db', 'proxy_db',
                'resume_lab_db', 'resume_artifact_root'}
    for key in required:
        raw = value.get(key)
        if not isinstance(raw, str):
            raise OpsError('review source path is missing: ' + key)
        path = Path(raw)
        prefix = Path('/var/lib/job-search')
        if not path.is_absolute() or '..' in path.parts or path == prefix or prefix not in path.parents:
            raise OpsError('review source path is outside the state mount: ' + key)
        target = state / path.relative_to(prefix)
        if target.resolve() != target:
            raise OpsError('review source path traverses a symlink: ' + key)
        result[key] = str(target)
    # Read gateway needs only these paths. Inference, mail, secrets and remote
    # tools are deliberately absent from the generated host application config.
    result['resume_mode'] = value.get('resume_mode', 'standard')
    return result


def _private(path):
    if path.is_symlink():
        raise OpsError('review host directory is a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.geteuid():
        raise OpsError('review host directory owner mismatch')
    path.chmod(0o700)
    return path


def _native_codex(c, image, run):
    root = _private(Path(c['data_root']) / 'operations' / 'review-bin')
    destination = root / image.rsplit(':', 1)[1]
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix='codex-', dir=root) as temporary:
            stage = Path(temporary)
            container = run(['docker', 'create', '--network', 'none', '--entrypoint', '/bin/true', image]).strip()
            if not re.fullmatch(r'[a-f0-9]{12,64}', container):
                raise OpsError('invalid Codex export container identity')
            try:
                run(['docker', 'cp', container + ':/opt/codex/.', str(stage)])
            finally:
                run(['docker', 'rm', '-f', container])
            _verify_native(stage)
            stage.joinpath('bin/codex').chmod(0o755)
            stage.joinpath('bin').chmod(0o755)
            stage.joinpath('SHA256SUMS').chmod(0o444)
            # TemporaryDirectory tolerates its path being atomically moved.
            os.replace(stage, destination)
    binary = _verify_native(destination)
    if run([str(binary), '--version']).strip() != 'codex-cli 0.160.0':
        raise OpsError('unexpected native Codex version')
    return binary


def _verify_native(root):
    if root.is_symlink() or root.stat().st_uid != os.geteuid():
        raise OpsError('unsafe native Codex directory')
    binary, receipt = root / 'bin/codex', root / 'SHA256SUMS'
    for item in (root / 'bin', binary, receipt):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise OpsError('unsafe native Codex export')
    if not binary.is_file() or not receipt.is_file() or receipt.stat().st_size > 256:
        raise OpsError('missing native Codex export')
    match = re.fullmatch(r'([a-f0-9]{64})  bin/codex\n', receipt.read_text())
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if not match or match[1] != digest:
        raise OpsError('native Codex checksum mismatch')
    return binary


def prepare(c, release, manifest, run, *, systemd_root=Path('/etc/systemd/system')):
    """Provision a pinned host runner, without signing in or enabling a schedule."""
    image = manifest.get('reviewer_image')
    if image is None:
        return {'status': 'not_in_release'}
    from .aws_ops import IMAGE
    if not IMAGE.fullmatch(image) or image.split('@', 1)[0] != manifest['app_image'].split('@', 1)[0]:
        raise OpsError('reviewer image must use the pinned application repository')
    d = Path(c['data_root'])
    binary = _native_codex(c, image, run)
    configs = _private(d / 'operations' / 'review-config' / manifest['release_id'])
    _private(d / 'operations' / 'review-auth')
    _private(d / 'runtime' / 'reviews')
    state = d / 'state' / 'review-runner'
    # Host maintenance repairs this subtree to root after restore/chown.
    _private(state)
    source = d / 'private' / 'config.json'
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 65536:
        raise OpsError('invalid review application configuration source')
    projection = application_projection(json.loads(source.read_text()), d, release)
    write_json(configs / 'application.json', projection)
    runner = {'version': 1, 'application_config': str(configs / 'application.json'),
              'state_dir': str(state), 'runtime_dir': str(d / 'runtime' / 'reviews'),
              'auth_home': str(d / 'operations' / 'review-auth'), 'model_image': image,
              'codex_executable': str(binary), 'model': 'gpt-6-astra', 'reasoning_effort': 'high',
              'codex_version': '0.160.0', 'worker_uid': 10001, 'worker_gid': 10001,
              'schedule_enabled': False, 'schedule_calendar': None}
    from .job_reviews.runner_config import RunnerConfig
    # Versioned release defaults, rather than edits to generated live settings.
    for name in ('batch_size', 'concurrency', 'assignment_timeout_seconds',
                 'invocation_timeout_seconds', 'transient_retries', 'preload_enabled',
                 'screening_enabled', 'screening_model', 'screening_reasoning_effort', 'screening_batch_size',
                 'benchmark_check_all'):
        runner[name] = getattr(RunnerConfig, name)
    write_json(configs / 'runner.json', runner)
    unit = release / 'deploy/aws' / UNIT
    if unit.is_symlink() or not unit.is_file():
        raise OpsError('release is missing review runner service')
    content = unit.read_text()
    for path in (str(d), str(c['release_root'])):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path):
            raise OpsError('review systemd paths require simple absolute paths')
    content = content.replace('/var/lib/job-search', str(d)).replace('/opt/job-search', c['release_root'])
    target = systemd_root / UNIT
    if target.is_symlink():
        raise OpsError('review service unit is a symlink')
    target.write_text(content)
    target.chmod(0o644)
    # No calendar is selected. Ship the timer template but do not install it.
    timer = systemd_root / TIMER
    if timer.exists() or timer.is_symlink():
        run(['systemctl', 'disable', '--now', TIMER])
        timer.unlink()
    run(['systemctl', 'daemon-reload'])
    return {'status': 'prepared', 'schedule_enabled': False, 'schedule_calendar': None}


def worker_containers(run):
    values = run(['docker', 'ps', '--filter', 'label=' + WORKER_LABEL, '--format', '{{.ID}}']).split()
    if any(not re.fullmatch(r'[a-f0-9]{12,64}', value) for value in values):
        raise OpsError('invalid isolated reviewer container identity')
    return values


def runner_busy(c):
    path = Path(c['data_root']) / 'state/review-runner/runner.lock'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OpsError('unsafe review runner lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def drain(c, run, *, timeout=660):
    """Called after the maintenance gate blocks new review invocations."""
    deadline = time.monotonic() + timeout
    while runner_busy(c) and time.monotonic() < deadline:
        time.sleep(0.25)
    if runner_busy(c):
        raise OpsError('review coordinator did not drain; state was not snapshotted')
    ids = worker_containers(run)
    if ids:
        run(['docker', 'stop', '--time', '30', *ids], timeout=60)
    if worker_containers(run):
        raise OpsError('isolated reviewer containers did not stop')


def status(c):
    """Return only operational counters, never model output, prompts or errors."""
    path = Path(c['data_root']) / 'state/review-runner/status.json'
    result = {'schedule_enabled': False, 'schedule_calendar': None, 'status': 'idle'}
    if not path.exists():
        return result
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        source = json.loads(path.read_text())
        for key in ('status', 'active', 'run_id', 'review_id', 'started_at', 'updated_at',
                    'completed_assignments', 'failed_assignments'):
            value = source.get(key)
            if value is None or type(value) in (str, int, bool):
                if value is not None:
                    result[key] = value
    except (OSError, ValueError, TypeError):
        result['status'] = 'unavailable'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operations-config', type=Path, default=Path('/etc/job-search/operations.json'))
    parser.add_argument('action', choices=('login', 'readiness', 'run', 'status'))
    args, remaining = parser.parse_known_args(argv)
    try:
        if os.geteuid() != 0:
            raise OpsError('review host launch requires the trusted root coordinator')
        from .aws_ops import load_config, release_path, manifest, verify_mount
        c = load_config(args.operations_config)
        verify_mount(c)
        release = release_path(c)
        m = manifest(release)
        if 'reviewer_image' not in m:
            raise OpsError('current release has no isolated reviewer runtime')
        config = Path(c['data_root']) / 'operations/review-config' / m['release_id'] / 'runner.json'
        os.environ['JOB_SEARCH_MAINTENANCE_GATE'] = str(Path(c['data_root']) / 'maintenance/gate.json')
        # No ambient OpenAI/API credentials pass to the coordinator authentication owner.
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
            os.environ.pop(key, None)
        from .job_reviews.runner import main as runner_main
        return runner_main([args.action, '--runner-config', str(config), *remaining])
    except (OpsError, OSError, ValueError):
        print(json.dumps({'status': 'failed', 'error': 'review host is not ready; inspect operations status'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
