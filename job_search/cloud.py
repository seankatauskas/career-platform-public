"""Cross-platform process loops for a single-host job-search deployment.

The macOS launchd integration intentionally remains the default local operator path.
This module supplies the equivalent long-running entry points for Linux containers:
one sequential loop for each bounded worker lane and signal-aware HTTP service loops.
SQLite leases remain the authority that prevents duplicate worker execution.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from .contracts import canonical_json, parse_utc
from .maintenance import draining, require_start
from .runtime import DEFAULT_CONFIG_PATH, RuntimeConfigV1, load_runtime_config
from .scheduler import utc_stamp
from .worker import (
    DEFAULT_MAX_OUTBOX_PER_TICK,
    DEFAULT_MAX_WORK_PER_TICK,
    WORK_LANES,
    _safe_error,
)


DEFAULT_TICK_INTERVAL_SECONDS = 5 * 60
MAX_CATCH_UP_TICKS = 3
CATCH_UP_INTERVAL_SECONDS = 1
DEFAULT_HTTP_POLL_SECONDS = 0.5
DEFAULT_HEALTH_DIR = Path("/tmp/job-search-health")
DEFAULT_IDLE_HEALTH_SECONDS = 15 * 60
DEFAULT_RUNNING_HEALTH_SECONDS = 70 * 60
HEALTH_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _health_path(directory: Path, lane: str) -> Path:
    if lane not in WORK_LANES:
        raise ValueError("lane must be core or model")
    return Path(directory) / f"worker-{lane}.json"


def _write_health(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically publish a small owner-only liveness record."""

    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except PermissionError:
        # A container may deliberately use a root-owned /tmp parent. The private
        # child and record still retain owner-only permissions.
        pass
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        encoded = (canonical_json(dict(value)) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _worker_health_record(
    lane: str,
    state: str,
    now: datetime,
    *,
    detail: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    allowed_states = {"starting", "running", "idle", "drained", "stopped", "failed"}
    if lane not in WORK_LANES or state not in allowed_states:
        raise ValueError("worker health state is invalid")
    record: dict[str, Any] = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "component": "worker",
        "lane": lane,
        "state": state,
        "updated_at": utc_stamp(now),
    }
    if detail:
        record["detail"] = dict(detail)
    return record


@contextmanager
def _shutdown_signals(stop: threading.Event) -> Iterator[None]:
    """Translate container stop signals into an interruptible shutdown event."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[signal.Signals, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_worker_loop(
    config: RuntimeConfigV1,
    *,
    lane: str,
    interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
    max_work: int = DEFAULT_MAX_WORK_PER_TICK,
    max_outbox: int = DEFAULT_MAX_OUTBOX_PER_TICK,
    health_dir: Path = DEFAULT_HEALTH_DIR,
    once: bool = False,
    stop: Optional[threading.Event] = None,
    runtime_factory: Optional[Callable[..., Any]] = None,
    now_provider: Callable[[], datetime] = _utc_now,
    emit: Callable[[str], None] = print,
) -> int:
    """Run bounded sequential ticks, briefly catching up when eligible work remains."""

    if lane not in WORK_LANES:
        raise ValueError("lane must be core or model")
    if not 1 <= interval_seconds <= 24 * 60 * 60:
        raise ValueError("interval_seconds must be between 1 second and 24 hours")
    if not 0 <= max_work <= 100 or not 0 <= max_outbox <= 100:
        raise ValueError("per-tick bounds must be between 0 and 100")
    if lane == "model" and max_outbox:
        # Worker itself forces this to zero; recording the effective value here
        # keeps the operator-facing contract unambiguous.
        max_outbox = 0
    if runtime_factory is None:
        from .runtime import build_runtime

        runtime_factory = build_runtime

    health_path = _health_path(health_dir, lane)
    stop_event = stop or threading.Event()
    started = now_provider()
    _write_health(health_path, _worker_health_record(lane, "starting", started))
    runtime = runtime_factory(
        config,
        lane=lane,
        max_work_per_tick=max_work,
        max_outbox_per_tick=max_outbox,
    )

    exit_code = 0
    catch_up_ticks = 0
    with _shutdown_signals(stop_event):
        while not stop_event.is_set():
            if draining():
                catch_up_ticks = 0
                _write_health(health_path, _worker_health_record(lane, "drained", now_provider()))
                stop_event.wait(1)
                continue
            tick_started = now_provider()
            _write_health(
                health_path,
                _worker_health_record(
                    lane,
                    "running",
                    tick_started,
                    detail={"tick_started_at": utc_stamp(tick_started)},
                ),
            )
            try:
                report = dict(runtime.tick(should_stop=lambda: stop_event.is_set() or draining()))
            except Exception as exc:
                failed = now_provider()
                error = _safe_error(exc)
                _write_health(
                    health_path,
                    _worker_health_record(
                        lane, "failed", failed, detail={"error": error}
                    ),
                )
                print(f"job-search {lane} worker failed: {error}", file=sys.stderr)
                exit_code = 1
                break
            completed = now_provider()
            summary = {
                "component": "worker",
                "lane": lane,
                "tick_started_at": utc_stamp(tick_started),
                "tick_completed_at": utc_stamp(completed),
                "report": report,
            }
            emit(canonical_json(summary))
            _write_health(
                health_path,
                _worker_health_record(
                    lane,
                    "idle",
                    completed,
                    detail={
                        "tick_completed_at": utc_stamp(completed),
                        "lease_acquired": bool(report.get("acquired")),
                    },
                ),
            )
            if once:
                break
            delay = interval_seconds
            if report.get("acquired") and report.get("more_due") and catch_up_ticks < MAX_CATCH_UP_TICKS:
                catch_up_ticks += 1
                delay = min(interval_seconds, CATCH_UP_INTERVAL_SECONDS)
            else:
                catch_up_ticks = 0
            # Observe maintenance promptly even while waiting between ticks.
            until = time.monotonic() + delay
            while not stop_event.is_set() and not draining() and time.monotonic() < until:
                stop_event.wait(min(1, max(0, until - time.monotonic())))

    if exit_code == 0:
        stopped = now_provider()
        _write_health(
            health_path,
            _worker_health_record(lane, "stopped", stopped),
        )
    return exit_code


def serve_http(
    config: RuntimeConfigV1,
    service: str,
    *,
    stop: Optional[threading.Event] = None,
    poll_seconds: float = DEFAULT_HTTP_POLL_SECONDS,
    bind_host: str = "127.0.0.1",
    allowed_hosts: Sequence[str] = (),
    server_factory: Optional[Callable[[RuntimeConfigV1], Any]] = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Serve dashboard or MCP while allowing SIGTERM to close its socket cleanly."""

    if service not in {"dashboard", "mcp", "interactions"}:
        raise ValueError("service must be dashboard, mcp or interactions")
    if not 0.05 <= poll_seconds <= 5:
        raise ValueError("poll_seconds must be between 0.05 and 5")
    if service == "dashboard" and (
        bind_host != "127.0.0.1" or allowed_hosts
    ):
        raise ValueError("dashboard binding is fixed to loopback")
    if server_factory is None:
        from .system import make_dashboard_host, make_mcp_host, make_interaction_host

        if service == "dashboard":
            server_factory = make_dashboard_host
        else:
            names = allowed_hosts or ("127.0.0.1", "localhost")
            factory = make_interaction_host if service == "interactions" else make_mcp_host
            server_factory = lambda value: factory(
                value,
                bind_host=bind_host,
                allowed_hosts=names,
            )
    server = server_factory(config)
    server.timeout = poll_seconds
    stop_event = stop or threading.Event()
    port = {"dashboard": config.dashboard_port, "mcp": config.mcp_port, "interactions": config.interaction_port}[service]
    emit(f"Job-search {service} listening on http://{bind_host}:{port}")
    try:
        with _shutdown_signals(stop_event):
            while not stop_event.is_set():
                server.handle_request()
    finally:
        server.server_close()
    return 0


def check_worker_health(
    path: Path,
    *,
    now: Optional[datetime] = None,
    max_idle_seconds: int = DEFAULT_IDLE_HEALTH_SECONDS,
    max_running_seconds: int = DEFAULT_RUNNING_HEALTH_SECONDS,
) -> bool:
    if max_idle_seconds < 1 or max_running_seconds < 1:
        raise ValueError("health windows must be positive")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            return False
        state = str(value.get("state") or "")
        if state not in {"starting", "running", "idle", "drained"}:
            return False
        updated = parse_utc(str(value.get("updated_at") or ""))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    age = ((now or _utc_now()) - updated).total_seconds()
    maximum = max_running_seconds if state in {"starting", "running"} else max_idle_seconds
    return -30 <= age <= maximum


def check_http_health(config: RuntimeConfigV1, service: str, timeout: float = 3) -> bool:
    if service not in {"dashboard", "mcp", "interactions"}:
        raise ValueError("service must be dashboard, mcp or interactions")
    port = {"dashboard": config.dashboard_port, "mcp": config.mcp_port, "interactions": config.interaction_port}[service]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        if service == "dashboard":
            connection.request(
                "GET", "/api/v1/health", headers={"Host": f"127.0.0.1:{port}"}
            )
        elif service == "interactions":
            from .system import read_mcp_token
            if config.interaction_token_file is None:
                return False
            connection.request("GET", "/health", headers={
                "Host": f"127.0.0.1:{port}",
                "Authorization": f"Bearer {read_mcp_token(config.interaction_token_file)}",
            })
        else:
            from .hermes_mcp import MCP_PROTOCOL_VERSION
            from .system import read_mcp_token

            body = canonical_json(
                {"jsonrpc": "2.0", "id": "healthcheck", "method": "ping"}
            )
            connection.request(
                "POST",
                "/mcp",
                body=body.encode("utf-8"),
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "Authorization": f"Bearer {read_mcp_token(config.mcp_token_file)}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                },
            )
        response = connection.getresponse()
        payload = response.read(64 * 1024)
        if response.status != 200:
            return False
        parsed = json.loads(payload.decode("utf-8"))
        if not isinstance(parsed, Mapping):
            return False
        if service == "dashboard":
            return str(parsed.get("status") or "") in {
                "healthy",
                "attention",
            }
        if service == "interactions":
            return parsed.get("status") in {"ok", "healthy", "ready"} or parsed.get("ok") is True
        return parsed.get("id") == "healthcheck" and isinstance(
            parsed.get("result"), Mapping
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the job-search control plane on a non-launchd host"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    commands = parser.add_subparsers(dest="command", required=True)

    worker = commands.add_parser("worker", help="run one bounded worker lane forever")
    worker.add_argument("--lane", choices=tuple(sorted(WORK_LANES)), required=True)
    worker.add_argument(
        "--interval-seconds", type=float, default=DEFAULT_TICK_INTERVAL_SECONDS
    )
    worker.add_argument("--max-work", type=int, default=DEFAULT_MAX_WORK_PER_TICK)
    worker.add_argument("--max-outbox", type=int, default=DEFAULT_MAX_OUTBOX_PER_TICK)
    worker.add_argument("--health-dir", type=Path, default=DEFAULT_HEALTH_DIR)
    worker.add_argument("--once", action="store_true")

    serve = commands.add_parser("serve", help="run one loopback HTTP service")
    serve.add_argument("service", choices=("dashboard", "mcp", "interactions"))
    serve.add_argument("--poll-seconds", type=float, default=DEFAULT_HTTP_POLL_SECONDS)
    serve.add_argument(
        "--bind-host",
        choices=("127.0.0.1", "0.0.0.0"),
        default="127.0.0.1",
    )
    serve.add_argument("--allowed-host", action="append", default=[])

    health = commands.add_parser("healthcheck", help="probe one local component")
    health.add_argument("component", choices=("dashboard", "mcp", "interactions", "core", "model"))
    health.add_argument("--health-dir", type=Path, default=DEFAULT_HEALTH_DIR)
    health.add_argument(
        "--max-idle-seconds", type=int, default=DEFAULT_IDLE_HEALTH_SECONDS
    )
    health.add_argument(
        "--max-running-seconds", type=int, default=DEFAULT_RUNNING_HEALTH_SECONDS
    )
    health.add_argument("--timeout", type=float, default=3)
    drain = commands.add_parser("draincheck", help="confirm a worker has no active task")
    drain.add_argument("lane", choices=("core", "model"))
    drain.add_argument("--health-dir", type=Path, default=DEFAULT_HEALTH_DIR)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "draincheck":
            record = json.loads(_health_path(args.health_dir, args.lane).read_text())
            return 0 if record.get("state") == "drained" else 1
        if args.command in {"worker", "serve"}:
            require_start(args.lane if args.command == "worker" else args.service)
        if args.command == "healthcheck" and args.component in WORK_LANES:
            healthy = check_worker_health(
                _health_path(args.health_dir, args.component),
                max_idle_seconds=args.max_idle_seconds,
                max_running_seconds=args.max_running_seconds,
            )
            return 0 if healthy else 1

        config = load_runtime_config(args.config, required=True)
        if args.command == "worker":
            return run_worker_loop(
                config,
                lane=args.lane,
                interval_seconds=args.interval_seconds,
                max_work=args.max_work,
                max_outbox=args.max_outbox,
                health_dir=args.health_dir,
                once=args.once,
            )
        if args.command == "serve":
            return serve_http(
                config,
                args.service,
                poll_seconds=args.poll_seconds,
                bind_host=args.bind_host,
                allowed_hosts=tuple(args.allowed_host),
            )
        if args.command == "healthcheck":
            return 0 if check_http_health(config, args.component, args.timeout) else 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"job-search cloud runtime: {_safe_error(exc)}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "check_http_health",
    "check_worker_health",
    "run_worker_loop",
    "serve_http",
]
