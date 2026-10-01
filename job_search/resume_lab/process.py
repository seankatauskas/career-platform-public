"""Small bounded subprocess runner shared by untrusted resume toolchains."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Sequence


class ProcessOutputLimitError(RuntimeError):
    """A child exceeded the combined stdout/stderr capture budget."""


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def run_bounded_process(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: str,
    env: Mapping[str, str],
    preexec_fn: Callable[[], Any],
    max_output_bytes: int,
) -> subprocess.CompletedProcess[str]:
    """Run a no-input child with an active combined pipe-output cap."""

    if not command or max_output_bytes < 1:
        raise ValueError("bounded process arguments are invalid")
    process = subprocess.Popen(
        tuple(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        cwd=cwd,
        env=dict(env),
        preexec_fn=preexec_fn,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    capture_lock = threading.Lock()
    overflow = threading.Event()
    reader_errors: list[OSError] = []

    def read_stream(stream: Any, name: str) -> None:
        try:
            while True:
                chunk = stream.read1(64 * 1024)
                if not chunk:
                    return
                with capture_lock:
                    used = len(buffers["stdout"]) + len(buffers["stderr"])
                    remaining = max_output_bytes - used
                    if len(chunk) > remaining:
                        if remaining > 0:
                            buffers[name].extend(chunk[:remaining])
                        overflow.set()
                    else:
                        buffers[name].extend(chunk)
                if overflow.is_set():
                    _terminate(process)
                    return
        except (OSError, ValueError) as exc:
            if not overflow.is_set():
                reader_errors.append(OSError(str(exc)))
        finally:
            try:
                stream.close()
            except OSError:
                pass

    readers = (
        threading.Thread(
            target=read_stream, args=(process.stdout, "stdout"), daemon=True
        ),
        threading.Thread(
            target=read_stream, args=(process.stderr, "stderr"), daemon=True
        ),
    )
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + float(timeout)
    try:
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate(process)
        process.wait()
        for reader in readers:
            reader.join(timeout=1)
        raise
    for reader in readers:
        reader.join(timeout=max(0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        _terminate(process)
        for reader in readers:
            reader.join(timeout=1)
        raise subprocess.TimeoutExpired(tuple(command), timeout)
    if overflow.is_set():
        raise ProcessOutputLimitError("child process output exceeded its limit")
    if reader_errors:
        raise OSError("child process output capture failed")
    return subprocess.CompletedProcess(
        tuple(command),
        returncode,
        bytes(buffers["stdout"]).decode("utf-8", "replace"),
        bytes(buffers["stderr"]).decode("utf-8", "replace"),
    )


__all__ = ["ProcessOutputLimitError", "run_bounded_process"]
