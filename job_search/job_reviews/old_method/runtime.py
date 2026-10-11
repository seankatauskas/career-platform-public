"""Reuse the Docker boundary and subscription gateway for an input-only lead."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import threading

from ...contracts import ContractError
from ..auth_owner import NativeAuthOwner
from ..codex_runtime import RuntimeConfig, WorkerHandle, docker_command, readiness, cleanup_inventory
from ..model_gateway import gateway_server
from ..runner import _private_dir, _write_json


@contextmanager
def diagnostic_log(root):
    """Host-only bounded JSONL plus a durable last-failure record, never raw payloads."""
    path = Path(root) / 'gateway.jsonl'
    lock = threading.Lock()
    # The attempt directory belongs to the host and is not mounted in the worker.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        written = 0
        truncated = False
        def record(value):
            nonlocal written, truncated
            encoded = json.dumps(value, separators=(',', ':'), allow_nan=False) + '\n'
            with lock:
                if value.get('event') == 'request_finished' and value.get('outcome') != 'completed':
                    _write_json(Path(root) / 'gateway-failure.json', value)
                size = len(encoded.encode())
                if written + size <= 4 * 1024 * 1024:
                    stream.write(encoded)
                    written += size
                elif not truncated:
                    stream.write('{"event":"log_limit_reached"}\n')
                    truncated = True
                stream.flush()
                os.fsync(stream.fileno())
        yield record


def read_output(path):
    """Worker files are untrusted, including links and special files."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64*1024*1024:
            raise ContractError('worker output is not a bounded regular file')
        return json.load(stream)


def command(config, name, output, model_socket, evidence):
    # Keep the existing network, UID, resource and capability restrictions.
    original = docker_command(config, name, output, model_socket, '/unused/review.sock',
                              grant_id=name, assignment_runtime_id=name)
    position = original.index(config.image)
    prefix = original[:position]
    mount = prefix.index('type=bind,source=/unused/review.sock,target=/review/review.sock,readonly')
    del prefix[mount-1:mount+1]
    if any(c in str(evidence) for c in (',', '\n')):
        raise ContractError('invalid evidence mount')
    return [*prefix, '--mount', f'type=bind,source={evidence},target=/evidence,readonly',
            '--entrypoint', 'python', config.image, '-m', 'job_search.job_reviews.old_method.entrypoint']


@contextmanager
def worker(config, packet, root, previous=None, telemetry=None):
    rc = RuntimeConfig(image=config.model_image, model=config.model, reasoning_effort=config.reasoning_effort,
                       worker_uid=config.worker_uid, worker_gid=config.worker_gid, review_contract_version=2,
                       memory='3g')
    if config.model != 'gpt-6-astra' or config.reasoning_effort != 'high':
        raise ContractError('old-method-v1 uses the frozen Astra high profile')
    if not readiness(rc)['ready']:
        raise ContractError('pinned review image is not ready')
    label = subprocess.run(['docker', 'image', 'inspect', '--format',
                            '{{index .Config.Labels "org.career-platform.review.old-method-version"}}', rc.image],
                           capture_output=True, text=True, check=True).stdout.strip()
    if label != '1':
        raise ContractError('review image does not support old-method-v1')
    cleanup_inventory(rc)
    root = _private_dir(Path(root))
    evidence, output = root / 'evidence', root / 'output'
    evidence.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    _write_json(evidence / 'input.json', packet)
    if previous is not None:
        _write_json(output / 'progress.json', previous)
    for path in (evidence, evidence/'input.json', output, *output.iterdir()):
        os.chown(path, config.worker_uid, config.worker_gid)
    # A short ephemeral socket path avoids AF_UNIX limits on per-release paths.
    sockets = _private_dir(config.runtime_dir / ('om_' + secrets.token_hex(6)))
    auth = NativeAuthOwner(config.auth_home, config.codex_executable)
    with diagnostic_log(root) as diagnostics, gateway_server(sockets/'model.sock', auth, model=rc.model, reasoning_effort=rc.reasoning_effort,
                        uid=rc.worker_uid, gid=rc.worker_gid, kind='old_method',
                        native_codex=True, telemetry_callback=telemetry, diagnostic_callback=diagnostics):
        name = 'career-review-old-' + secrets.token_hex(8)
        streams = [(root / p).open('wb') for p in ('codex.jsonl', 'stderr.log')]
        handle = None
        try:
            process = subprocess.Popen(command(rc, name, output, sockets/'model.sock', evidence),
                                       stdin=subprocess.DEVNULL, stdout=streams[0], stderr=streams[1], start_new_session=True)
            handle = WorkerHandle(process, rc, {'image_digest': rc.image, 'runtime_id': name}, streams)
            handle.runtime_id, handle.output_directory = name, output
            yield handle
        finally:
            if handle is not None:
                if handle.poll() is None:
                    handle.terminate()
                else:
                    handle.wait()
            for stream in streams:
                stream.close()
    sockets.rmdir()
