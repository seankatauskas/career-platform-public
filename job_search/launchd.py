"""Pure launchd rendering plus explicit, opt-in local service management."""

from __future__ import annotations

import hashlib
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .runtime import DEFAULT_CONFIG_PATH, RuntimeConfigV1


SERVICE_INTERVAL_SECONDS = 5 * 60
SERVICE_RESTART_THROTTLE_SECONDS = 30
SERVICE_LABELS = {
    "core": "com.local.job-search-core",
    "model": "com.local.job-search-model",
    "dashboard": "com.local.job-search-dashboard",
    "mcp": "com.local.job-search-mcp",
}
LONG_RUNNING_SERVICES = frozenset({"dashboard", "mcp"})


def _config_path(config: RuntimeConfigV1, supplied: Optional[Path]) -> Path:
    return (supplied or config.source_path or DEFAULT_CONFIG_PATH).expanduser().resolve()


def render_launch_agents(
    config: RuntimeConfigV1,
    *,
    config_path: Optional[Path] = None,
    python_executable: Optional[Path] = None,
) -> Mapping[str, bytes]:
    """Render four secret-free plist documents without touching the filesystem."""

    source = _config_path(config, config_path)
    # Preserve the venv entry point: resolving its symlink runs the base Python
    # without the installation's dependencies under launchd.
    executable = (python_executable or Path(sys.executable)).expanduser().absolute()
    result = {}
    for service, label in SERVICE_LABELS.items():
        if service in LONG_RUNNING_SERVICES:
            arguments = [
                str(executable),
                "-m",
                "job_search.system",
                "--config",
                str(source),
                service,
            ]
        else:
            arguments = [
                str(executable),
                "-m",
                "job_search.worker",
                "--config",
                str(source),
                "--lane",
                service,
            ]
        value = {
            "Label": label,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(config.project_root),
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": str(config.log_dir / f"{service}.stdout.log"),
            "StandardErrorPath": str(config.log_dir / f"{service}.stderr.log"),
        }
        if service in LONG_RUNNING_SERVICES:
            value["KeepAlive"] = True
            value["ThrottleInterval"] = SERVICE_RESTART_THROTTLE_SECONDS
        else:
            value["StartInterval"] = SERVICE_INTERVAL_SECONDS
        result[service] = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)
    return result


def launch_agent_plan(
    config: RuntimeConfigV1,
    *,
    action: str,
    output_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    python_executable: Optional[Path] = None,
) -> Mapping[str, Any]:
    if action not in {"install", "uninstall", "status"}:
        raise ValueError("unknown service action")
    destination = (
        output_dir or Path.home() / "Library" / "LaunchAgents"
    ).expanduser().resolve()
    source = _config_path(config, config_path)
    rendered = render_launch_agents(
        config,
        config_path=source,
        python_executable=python_executable,
    )
    domain = f"gui/{os.getuid()}"
    services = []
    for lane, label in SERVICE_LABELS.items():
        path = destination / f"{label}.plist"
        if action == "install":
            command = ["launchctl", "bootstrap", domain, str(path)]
        elif action == "uninstall":
            command = ["launchctl", "bootout", f"{domain}/{label}"]
        else:
            command = ["launchctl", "print", f"{domain}/{label}"]
        services.append(
            {
                "lane": lane,
                "label": label,
                "path": str(path),
                "sha256": hashlib.sha256(rendered[lane]).hexdigest(),
                "command": command,
            }
        )
    return {
        "schema_version": 1,
        "action": action,
        "config_path": str(source),
        "output_dir": str(destination),
        "services": services,
    }


def _atomic_private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def manage_launch_agents(
    config: RuntimeConfigV1,
    *,
    action: str,
    apply: bool = False,
    output_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    python_executable: Optional[Path] = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Mapping[str, Any]:
    """Plan by default; mutate launchd only after the caller explicitly sets apply."""

    plan = dict(
        launch_agent_plan(
            config,
            action=action,
            output_dir=output_dir,
            config_path=config_path,
            python_executable=python_executable,
        )
    )
    plan["applied"] = False
    if not apply:
        return plan
    if action == "status":
        raise ValueError("status is read-only and does not accept --apply")
    source = Path(str(plan["config_path"]))
    if action == "install" and not source.is_file():
        raise ValueError("install requires an existing owner-only runtime config")
    if action == "install" and os.stat(source).st_mode & 0o077:
        raise ValueError("install requires an owner-only runtime config")
    if action == "install":
        protected = [Path.home() / name for name in ("Documents", "Desktop", "Downloads")]
        executable = (python_executable or Path(sys.executable)).expanduser().absolute()
        paths = (config.project_root, executable, source)
        if any(base == path or base in path.parents
               for value in paths for path in (value, value.resolve()) for base in protected):
            raise ValueError(
                "background services must run outside Documents, Desktop and Downloads; "
                "install a dedicated runtime and venv under ~/.local/share/career-platform/runtime "
                "instead of granting macOS folder access"
            )
        from .system import read_mcp_token

        try:
            read_mcp_token(config.mcp_token_file)
        except (OSError, ValueError) as exc:
            raise ValueError(
                "install requires a valid owner-only MCP token; run mcp-token-init"
            ) from exc
        if config.hermes_telegram_target:
            if config.hermes_notification_socket is not None:
                from .hermes_delivery import (
                    SERVICE_REVISION,
                    HermesDeliveryBridgeError,
                    HermesDeliveryClient,
                )

                try:
                    report = HermesDeliveryClient(
                        config.hermes_notification_socket, timeout_seconds=3
                    ).ping()
                except HermesDeliveryBridgeError as exc:
                    raise ValueError(
                        "notification delivery requires a ready Hermes socket"
                    ) from exc
                if report.get("service_revision") != SERVICE_REVISION:
                    raise ValueError(
                        "notification delivery requires a ready Hermes socket"
                    )
            else:
                executable = config.hermes_executable
                if (
                    executable is None
                    or not executable.is_file()
                    or not os.access(executable, os.X_OK)
                ):
                    raise ValueError(
                        "notification delivery requires an executable hermes_executable"
                    )
    services = list(plan["services"])
    domain = f"gui/{os.getuid()}"
    outcomes: list[dict[str, Any]] = []

    def invoke(service: Mapping[str, Any], operation: str, command: tuple[str, ...]) -> int:
        try:
            completed = runner(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
            )
        except OSError as exc:
            outcomes.append(
                {
                    "label": service["label"],
                    "operation": operation,
                    "returncode": 1,
                    "stdout": "",
                    "stderr": str(exc)[-2000:],
                }
            )
            return 1
        outcomes.append(
            {
                "label": service["label"],
                "operation": operation,
                "returncode": int(completed.returncode),
                "stdout": str(completed.stdout or "")[-2000:],
                "stderr": str(completed.stderr or "")[-2000:],
            }
        )
        return int(completed.returncode)

    loaded: dict[str, bool] = {}
    snapshots: dict[str, tuple[bool, bytes, int]] = {}
    for service in services:
        label = str(service["label"])
        target = f"{domain}/{label}"
        loaded[label] = invoke(
            service, "inspect", ("launchctl", "print", target)
        ) == 0
        path = Path(str(service["path"]))
        existed = path.exists()
        snapshots[label] = (
            existed,
            path.read_bytes() if existed else b"",
            path.stat().st_mode & 0o777 if existed else 0o600,
        )

    def restore_files() -> bool:
        restored = True
        for service in services:
            label = str(service["label"])
            path = Path(str(service["path"]))
            existed, content, mode = snapshots[label]
            try:
                if existed:
                    _atomic_private_write(path, content)
                    os.chmod(path, mode)
                elif path.exists():
                    path.unlink()
            except OSError as exc:
                restored = False
                outcomes.append(
                    {
                        "label": label,
                        "operation": "restore-file",
                        "returncode": 1,
                        "stdout": "",
                        "stderr": str(exc)[-2000:],
                    }
                )
        return restored

    def restart_previous(stopped: set[str]) -> bool:
        restarted = True
        for service in services:
            label = str(service["label"])
            if label not in stopped:
                continue
            path = Path(str(service["path"]))
            if invoke(
                service,
                "rollback-bootstrap",
                ("launchctl", "bootstrap", domain, str(path)),
            ):
                restarted = False
        return restarted

    stopped: set[str] = set()
    ok = True
    if action == "install":
        rendered = render_launch_agents(
            config,
            config_path=source,
            python_executable=python_executable,
        )
        config.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(config.log_dir, 0o700)
        try:
            for service in services:
                _atomic_private_write(
                    Path(str(service["path"])), rendered[str(service["lane"])]
                )
        except Exception:
            restore_files()
            raise

        for service in services:
            label = str(service["label"])
            if not loaded[label]:
                continue
            if invoke(
                service,
                "bootout",
                ("launchctl", "bootout", f"{domain}/{label}"),
            ):
                ok = False
                break
            stopped.add(label)

        started: set[str] = set()
        if ok:
            for service in services:
                label = str(service["label"])
                if invoke(
                    service,
                    "bootstrap",
                    ("launchctl", "bootstrap", domain, str(service["path"])),
                ):
                    ok = False
                    break
                started.add(label)

        if not ok:
            for service in reversed(services):
                label = str(service["label"])
                if label in started:
                    invoke(
                        service,
                        "rollback-bootout",
                        ("launchctl", "bootout", f"{domain}/{label}"),
                    )
            restore_files()
            restart_previous(stopped)
    else:
        for service in services:
            label = str(service["label"])
            if not loaded[label]:
                continue
            if invoke(
                service,
                "bootout",
                ("launchctl", "bootout", f"{domain}/{label}"),
            ):
                ok = False
                break
            stopped.add(label)
        if ok:
            try:
                for service in services:
                    path = Path(str(service["path"]))
                    if path.exists():
                        path.unlink()
            except OSError as exc:
                outcomes.append(
                    {
                        "label": str(service["label"]),
                        "operation": "remove-file",
                        "returncode": 1,
                        "stdout": "",
                        "stderr": str(exc)[-2000:],
                    }
                )
                ok = False
        if not ok:
            restore_files()
            restart_previous(stopped)

    plan["applied"] = True
    plan["outcomes"] = outcomes
    plan["ok"] = ok
    return plan


def service_status(
    config: RuntimeConfigV1,
    *,
    output_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Mapping[str, Any]:
    plan = dict(
        launch_agent_plan(
            config,
            action="status",
            output_dir=output_dir,
            config_path=config_path,
        )
    )
    statuses = []
    for service in plan["services"]:
        completed = runner(
            tuple(service["command"]),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
        statuses.append(
            {
                "label": service["label"],
                "loaded": completed.returncode == 0,
                "detail": str(completed.stdout or completed.stderr or "")[-2000:],
            }
        )
    plan["services"] = statuses
    return plan
