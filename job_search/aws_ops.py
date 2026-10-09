"""Single-host AWS operations. Host-only, stdlib-only; never mounted into Hermes.

All mutations serialize on a root-owned lock. Backups require a quiesced stack,
restore verifies an entire bundle before replacing any state, and releases remain
paused until explicit activation. subprocess errors deliberately omit output.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}\Z")
SECRET_FILES = {"config.json", "inference.json", "resume-model.json", "runpod-api-key", "mcp-token", "portable-master-key", "hermes.env", "hermes.yaml", "tailscale-auth-key", "interaction-token", "briefing-inference.json", "briefing-api-key"}
SERVICES = ("tools", "dashboard", "mcp", "core", "model", "hermes")
REVIEW_SERVICES = ("tools", "dashboard", "mcp", "hermes")
BACKUP_DIRS = ("state", "hermes", "toolchain")
# Credential-bearing Hermes files are recovered from Secrets Manager, not the bundle.
EXCLUDED = {".env", "config.yaml", ".operations.lock", "runtime", "logs", "__pycache__"}
_STATUS_DEADLINE = ContextVar("status_deadline", default=None)
MAINTENANCE_GRACE_SECONDS = 3600

from .operation_journal import (
    OpsError, Operation, read as read_operation, require_idle, set_gate as _set_gate,
    write_json, sync_directory, sync_tree, now as operation_now,
)


def set_gate(c, allowed, *, draining=False, initialize=None):
    permitted = list(allowed)
    if not draining and "dashboard" in permitted:
        try:
            if manifest(release_path(c)).get("reviewer_image"):
                permitted.append("review-runner")
        except (OpsError, OSError, ValueError):
            pass
    _set_gate(c, permitted, draining=draining, initialize=initialize)


class OperationBusy(OpsError):
    """Positive lock contention, distinct from unreadable maintenance state."""


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def load_config(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise OpsError("operations config must be a private regular file")
    if info.st_uid != os.geteuid(): raise OpsError("operations config owner mismatch")
    c = json.loads(path.read_text())
    required = {"version", "aws_region", "backup_bucket", "release_bucket", "data_root", "release_root", "data_volume_id", "secret_arns"}
    if not required <= c.keys() or c["version"] != 1: raise OpsError("invalid operations configuration")
    for key in ("data_root", "release_root"):
        raw = Path(c[key])
        if not raw.is_absolute() or raw == Path("/") or ".." in raw.parts or raw.resolve() != raw:
            raise OpsError("operation roots must be canonical absolute paths")
    data, release = Path(c["data_root"]), Path(c["release_root"])
    if data == release or data in release.parents or release in data.parents:
        raise OpsError("data and release roots must be disjoint")
    for name in ("backup_bucket", "release_bucket"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", c[name]): raise OpsError("invalid bucket")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", c["aws_region"]): raise OpsError("invalid region")
    if not re.fullmatch(r"vol-[a-f0-9]+", c["data_volume_id"]): raise OpsError("invalid data volume")
    if not isinstance(c["secret_arns"], dict) or set(c["secret_arns"]) - SECRET_FILES:
        raise OpsError("unknown secret file")
    for arn in c["secret_arns"].values():
        if not isinstance(arn, str) or not re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+", arn):
            raise OpsError("invalid secret reference")
    return c

def run(argv: list[str], *, timeout: int = 300, env: dict | None = None) -> str:
    deadline = _STATUS_DEADLINE.get()
    if deadline is not None:
        timeout = min(timeout, 50, deadline - time.monotonic())
        if timeout <= 0:
            raise OpsError("status diagnostic deadline exceeded")
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout, env=env)
        return result.stdout
    except (subprocess.SubprocessError, OSError):
        raise OpsError("external command failed: " + Path(argv[0]).name) from None

def aws(c: dict, *args: str) -> str:
    return run(["aws", "--region", c["aws_region"], "--no-cli-pager", *args], timeout=1800)

def verify_mount(c: dict) -> None:
    root = Path(c["data_root"])
    if not os.path.ismount(root): raise OpsError("persistent data volume is not mounted")
    marker = root / ".job-search-volume-id"
    if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != c["data_volume_id"]:
        raise OpsError("persistent data volume identity mismatch")

@contextmanager
def lock(c: dict):
    verify_mount(c)
    path = Path(c["data_root"]) / ".operations.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise OperationBusy("another deployment or backup is active") from None
        yield
    finally: os.close(fd)


def operation_lock_held(c: dict) -> bool | None:
    """Observe maintenance ownership without creating a lock file or waiting.

    None means the lock could not be inspected safely, not proof of an active
    deployment. The journal still blocks all competing mutations in that case.
    """
    try: fd = os.open(Path(c["data_root"]) / ".operations.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError: return False
    except OSError: return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            return None
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
        except OSError: return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally: os.close(fd)

def release_path(c: dict, release_id: str | None = None) -> Path:
    root = Path(c["release_root"])
    target = root / "current" if release_id is None else root / "releases" / valid_id(release_id)
    actual = target.resolve()
    if actual.parent != root / "releases" or not actual.is_dir(): raise OpsError("release is not installed")
    return actual

def valid_id(value: str) -> str:
    if not ID.fullmatch(value) or value in {".", ".."}: raise OpsError("invalid release or backup ID")
    return value

def manifest(path: Path) -> dict:
    m = json.loads((path / "release.json").read_text())
    if m.get("version") != 1 or not ID.fullmatch(m.get("release_id", "")): raise OpsError("invalid release manifest")
    for key in ("app_image", "hermes_image", "hermes_base_image"):
        if not IMAGE.fullmatch(m.get(key, "")): raise OpsError("release images must be pinned by digest")
    if "reviewer_image" in m and (not IMAGE.fullmatch(m["reviewer_image"]) or m["reviewer_image"].split("@", 1)[0] != m["app_image"].split("@", 1)[0]):
        raise OpsError("reviewer image must use the pinned application repository")
    if not isinstance(m.get("tectonic_version"), str) or not m["tectonic_version"]: raise OpsError("missing Tectonic version")
    return m

def compose(c: dict, *args: str, release: Path | None = None) -> str:
    r = release or release_path(c); m = manifest(r); d = Path(c["data_root"])
    # Sanitized host observations only; never mount billing credentials here.
    costs = d / "costs"
    if costs.is_symlink(): raise OpsError("cost observation directory is a symlink")
    if args and args[0] in {"up", "run"}:
        # Compose ps/config and status are diagnostics, not provisioning paths.
        costs.mkdir(mode=0o755, exist_ok=True)
        costs.chmod(0o755)
        if os.geteuid() == 0: os.chown(costs, 0, c.get("app_gid", 10001))
    env = dict(os.environ)
    env.update({
        "JOB_SEARCH_MAINTENANCE_DIR": str(d / "maintenance"),
        "JOB_SEARCH_COST_DIR": str(costs),
        "JOB_SEARCH_IMAGE": m["app_image"], "JOB_SEARCH_HERMES_BRIDGE_IMAGE": m["hermes_image"],
        "JOB_SEARCH_HERMES_BASE_IMAGE": m["hermes_base_image"], "JOB_SEARCH_STATE_DIR": str(d / "state"),
        "JOB_SEARCH_PRIVATE_DIR": str(d / "private"), "JOB_SEARCH_HERMES_DATA_DIR": str(d / "hermes"),
        "JOB_SEARCH_TOOLCHAIN_DIR": str(d / "toolchain"), "JOB_SEARCH_TECTONIC_VERSION": m["tectonic_version"],
        "JOB_SEARCH_TOOL_RUNTIME_DIR": str(d / "runtime" / "tools"),
        "JOB_SEARCH_NOTIFICATION_RUNTIME_DIR": str(d / "runtime" / "notifications"),
        "JOB_SEARCH_MCP_TOKEN_FILE": str(d / "private" / "mcp-token"),
        "JOB_SEARCH_NOTIFICATION_TARGET": "telegram", "JOB_SEARCH_UID": str(c.get("app_uid", 10001)),
        "JOB_SEARCH_GID": str(c.get("app_gid", 10001)),
    })
    files = ["-f", str(r / "compose.cloud.yaml"), "-f", str(r / "compose.hermes.yaml")]
    runtime_path = d / "private" / "config.json"
    if runtime_path.is_file():
        runtime = json.loads(runtime_path.read_text())
        if runtime.get("mail_inference_config") and (r / "compose.mail.yaml").is_file():
            if runtime["mail_inference_config"] != "/run/job-search/mail-inference.json":
                raise OpsError("cloud mail inference requires the dedicated mounted profile")
            files += ["-f", str(r / "compose.mail.yaml")]
        if runtime.get("briefing_inference_config") and (r / "compose.briefing.yaml").is_file():
            if runtime["briefing_inference_config"] != "/run/job-search/briefing-inference.json":
                raise OpsError("cloud briefings require the dedicated mounted profile")
            files += ["-f", str(r / "compose.briefing.yaml")]
        if runtime.get("interaction_token_file") and (r / "compose.chief.yaml").is_file():
            if runtime["interaction_token_file"] != "/run/job-search/interaction-token" or runtime.get("interaction_port", 8768) != 8768:
                raise OpsError("cloud interactions require the dedicated mounted bearer and port 8768")
            for suffix in ("BOT", "USER", "CHAT"):
                identity = runtime.get("telegram_" + suffix.lower() + "_id", "")
                if not isinstance(identity, str) or not re.fullmatch(r"[1-9][0-9]{0,23}", identity):
                    raise OpsError("cloud interactions require explicit Telegram owner identity")
                env["JOB_SEARCH_TELEGRAM_" + suffix + "_ID"] = identity
            files += ["-f", str(r / "compose.chief.yaml")]
    return run(["docker", "compose", "--project-name", "job-search", *files, *args], env=env, timeout=4500)

def running_services(c: dict, release: Path | None = None) -> list[str]:
    return [v for v in compose(c, "ps", "--services", "--status", "running", release=release).splitlines() if v in (*SERVICES, "interactions")]


def configured_services(c: dict, *, review: bool = False, release: Path | None = None) -> list[str]:
    services = list(REVIEW_SERVICES if review else SERVICES)
    runtime_path = Path(c["data_root"]) / "private" / "config.json"
    if runtime_path.is_file() and json.loads(runtime_path.read_text()).get("interaction_token_file"):
        if ((release or release_path(c)) / "compose.chief.yaml").is_file():
            services.append("interactions")
    return services

def activation(c: dict) -> dict:
    path = Path(c["data_root"]) / "activation.json"
    return json.loads(path.read_text()) if path.exists() else {"enabled": False}


def available_space(c: dict, *, restore_bytes: int | None = None, local_snapshot: bool = False) -> None:
    root = Path(c["data_root"])
    # Local snapshots need capture + independent recovery staging. Off-host
    # bundles additionally need archive space. Never spend recovery headroom.
    # At restore time the snapshot already occupies disk; only staging is new.
    if restore_bytes is not None:
        needed = restore_bytes
    else:
        size = sum(p.stat().st_size for name in BACKUP_DIRS for p in (root / name).rglob("*") if p.is_file() and not p.is_symlink())
        needed = size * (2 if local_snapshot else 3)
    needed += 256 * 1024**2
    if shutil.disk_usage(root).free < needed:
        raise OpsError("insufficient disk space for a recoverable maintenance operation")


def drain_workers(c: dict, active: list[str], timeout: int = 4200) -> None:
    lanes = [s for s in active if s in {"core", "model"}]
    set_gate(c, [s for s in active if s not in lanes], draining=True)
    from .review_host import drain as drain_reviews
    drain_reviews(c, run)
    if not lanes:
        return
    until = time.monotonic() + timeout
    waiting = set(lanes)
    while waiting and time.monotonic() < until:
        for lane in list(waiting):
            try:
                compose(c, "exec", "-T", lane, "python", "-m", "job_search.cloud", "draincheck", lane)
                waiting.remove(lane)
            except OpsError:
                pass
        if waiting:
            time.sleep(1)
    if waiting:
        set_gate(c, active)
        raise OpsError("worker drain deadline exceeded; no database changes made")
    compose(c, "stop", "--timeout", "30", *lanes)


def stop_for_maintenance(c: dict, op: Operation, active: list[str]) -> None:
    op.update("draining")
    try:
        drain_workers(c, active)
    except Exception:
        set_gate(c, active)
        op.finish("drain_aborted")
        raise
    op.update("stopping", downtime_started_at=operation_now())
    set_gate(c, [], draining=True)
    remaining = active  # workers have already acknowledged a safe drain
    if remaining:
        compose(c, "stop", "--timeout", "30", *remaining)
    # The first deployment has no current release for Compose to resolve. Inspect
    # Docker's project labels directly, also catching stray one-shot writers.
    if project_containers():
        raise OpsError("writers failed to stop; recover the operation")
    op.update("quiesced")


@contextmanager
def quiesced(c: dict):
    active = running_services(c)
    op = Operation.begin(c, "backup", previous_release=release_path(c).name,
                         active_services=active, previous_activation=activation(c))
    try:
        stop_for_maintenance(c, op, active)
        yield
    finally:
        # A normal exception can resume a consistent snapshot source. SIGKILL
        # leaves the journal incomplete and startup gated until explicit recovery.
        op.update("resuming", writes_possible=True)
        set_gate(c, active)
        if active:
            compose(c, "up", "-d", "--no-deps", "--no-build", *active)
            wait_healthy(c, active)
        op.finish("snapshot_finished", downtime_finished_at=operation_now())

def sqlite_check(path: Path) -> None:
    with path.open("rb") as f: signature = f.read(16)
    if signature != b"SQLite format 3\x00": return
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        if con.execute("PRAGMA quick_check").fetchall() != [("ok",)]: raise OpsError("database integrity failed")
    finally: con.close()

def runtime_entries(source: Path, *, hermes: bool = False, snapshot: bool = False):
    """Walk without following links; prune only the rebuildable Hermes uv cache."""
    def visit(directory):
        for item in directory.iterdir():
            rel = item.relative_to(source)
            if snapshot and item.name in EXCLUDED: continue
            info = item.lstat()
            if hermes and rel == Path("home/.cache/uv"):
                if not stat.S_ISDIR(info.st_mode):
                    raise OpsError("Hermes uv cache must be a real directory")
                # uv wheel entries are symlinks, not durable application state.
                # Keep ownership on the cache root without walking its contents.
                if not snapshot: yield item, info
                continue
            yield item, info
            if stat.S_ISDIR(info.st_mode): yield from visit(item)
    yield from visit(source)


def copy_snapshot(source: Path, target: Path, *, hermes: bool = False) -> None:
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    if source.is_symlink(): raise OpsError("snapshot directory is a symlink")
    if not source.exists(): return
    for item, info in runtime_entries(source, hermes=hermes, snapshot=True):
        rel = item.relative_to(source)
        if hermes and stat.S_ISSOCK(info.st_mode) and (
            rel == Path("gateway.sock") or
            (rel.parent == Path("state") and re.fullmatch(r"gateway\.loop-tick\.[0-9]+\.sock", rel.name))
        ): continue
        if stat.S_ISDIR(info.st_mode):
            (target / rel).mkdir(parents=True, mode=0o700, exist_ok=True)
        elif stat.S_ISREG(info.st_mode):
            if item.name.endswith(("-wal", "-shm")): continue
            dest = target / rel; dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with item.open("rb") as stream: is_database = stream.read(16) == b"SQLite format 3\x00"
            if is_database:
                source_db = sqlite3.connect(item.as_uri() + "?mode=ro", uri=True)
                target_db = sqlite3.connect(dest)
                try: source_db.backup(target_db)
                finally: source_db.close(); target_db.close()
            else: shutil.copy2(item, dest)
            dest.chmod(0o600)
        else: raise OpsError("snapshot contains a symlink or special file")
    for item in target.rglob("*"):
        if item.is_file() and not item.name.endswith(("-wal", "-shm")): sqlite_check(item)

def secret_versions(c: dict) -> dict:
    # Record the versions actually used with this state, never mutable AWSCURRENT.
    result = json.loads((Path(c["data_root"]) / "materialized-secrets.json").read_text())
    if set(result) != set(c["secret_arns"]): raise OpsError("materialized secret inventory mismatch")
    for name, ref in result.items():
        if ref.get("arn") != c["secret_arns"][name] or not ref.get("version_id"):
            raise OpsError("materialized secret version missing")
    return result

def backup_unlocked(c: dict, *, paused: bool = False, upload: bool = True, release_id: str | None = None) -> dict:
    if paused and not upload:
        return local_snapshot_unlocked(c, release_id=release_id)
    available_space(c)
    snapshot_at = operation_now()
    bid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(4).hex()
    d = Path(c["data_root"]); backups = d / "backups"; backups.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="backup-", dir=backups) as temp:
        stage = Path(temp) / "payload"; stage.mkdir(mode=0o700)
        versions = {}
        def capture():
            nonlocal snapshot_at
            snapshot_at = operation_now()
            versions.update(secret_versions(c))
            for name in BACKUP_DIRS: copy_snapshot(d / name, stage / name, hermes=name == "hermes")
        if paused: capture()
        else:
            with quiesced(c): capture()
        files = {str(p.relative_to(stage)): {"sha256": digest(p), "size": p.stat().st_size}
                 for p in stage.rglob("*") if p.is_file()}
        m = {"version": 1, "backup_id": bid, "created_at": snapshot_at, "snapshot_at": snapshot_at,
             "release_id": release_id or release_path(c).name, "secrets": versions, "files": files}
        write_json(stage / "backup.json", m)
        bundle = Path(temp) / "backup.tar.gz"
        # Routine off-host backup compression still runs after services resume.
        with tarfile.open(bundle, "w:gz", compresslevel=9) as tar:
            for p in sorted(stage.rglob("*")):
                tar.add(p, arcname=str(p.relative_to(stage)), recursive=False)
        sync_tree(Path(temp))
        archive_hash = digest(bundle)
        if upload:
            key = f"backups/{bid}/backup.tar.gz"
            aws(c, "s3", "cp", str(bundle), f"s3://{c['backup_bucket']}/{key}", "--only-show-errors")
            receipt = {"version": 1, "backup_id": bid, "sha256": archive_hash, "created_at": m["created_at"], "snapshot_at": snapshot_at, "uploaded_at": operation_now(), "release_id": m["release_id"]}
            write_json(Path(temp) / "receipt.json", receipt)
            aws(c, "s3", "cp", str(Path(temp) / "receipt.json"), f"s3://{c['backup_bucket']}/backups/{bid}/receipt.json", "--only-show-errors")
            write_json(d / "last-backup.json", receipt)
        else:
            destination = backups / (bid + ".tar.gz")
            shutil.copy2(bundle, destination)
            with destination.open("rb") as stream: os.fsync(stream.fileno())
            sync_directory(backups)
        return {"status": "backed_up", "backup_id": bid, "sha256": archive_hash}


def backup_root(c: dict) -> Path:
    root = Path(c["data_root"]) / "backups"
    if root.is_symlink(): raise OpsError("backup directory is a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    if root.stat().st_uid != os.geteuid(): raise OpsError("backup directory owner mismatch")
    return root


def local_snapshot_unlocked(c: dict, *, release_id: str | None = None) -> dict:
    """Durable local rollback without compression or a second full archive copy.

    All writers must already be stopped and the operations lock held. Only a
    fully fsynced snapshot is atomically published; migration starts only after
    its manifest digest is durably recorded in the operation journal. The SHA
    commits to every retained file's checksum, not to a compressed byte stream.
    """
    started = time.monotonic()
    available_space(c, local_snapshot=True)
    d = Path(c["data_root"]); backups = backup_root(c)
    bid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(4).hex()
    snapshot_at = operation_now()
    stage = Path(tempfile.mkdtemp(prefix="snapshot-pending-", dir=backups))
    published = backups / (bid + ".snapshot")
    try:
        versions = secret_versions(c)
        for name in BACKUP_DIRS: copy_snapshot(d / name, stage / name, hermes=name == "hermes")
        captured = time.monotonic()
        files = {str(p.relative_to(stage)): {"sha256": digest(p), "size": p.stat().st_size}
                 for p in stage.rglob("*") if p.is_file()}
        directories = sorted(str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_dir())
        m = {"version": 1, "format": "directory-v1", "backup_id": bid,
             "created_at": snapshot_at, "snapshot_at": snapshot_at,
             "release_id": release_id or release_path(c).name, "secrets": versions,
             "files": files, "directories": directories}
        write_json(stage / "backup.json", m)
        manifest_hash = digest(stage / "backup.json")
        manifested = time.monotonic()
        sync_tree(stage)
        if published.exists() or published.is_symlink(): raise OpsError("backup ID already exists")
        os.replace(stage, published)
        sync_directory(backups)
        finished = time.monotonic()
        return {"status": "backed_up", "format": "directory-v1", "backup_id": bid,
                "sha256": manifest_hash, "snapshot_at": snapshot_at,
                "file_count": len(files), "size_bytes": sum(f["size"] for f in files.values()),
                "timings_seconds": {"capture": round(captured - started, 3),
                                    "manifest": round(manifested - captured, 3),
                                    "durable_publish": round(finished - manifested, 3),
                                    "total": round(finished - started, 3)}}
    finally:
        # A crash leaves an unpublished directory for inspection, never a valid
        # rollback receipt. Ordinary failures can remove only our own staging.
        if stage.exists(): shutil.rmtree(stage)


def local_backup_path(c: dict, snapshot: dict) -> Path:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("backup_id"), str):
        raise OpsError("invalid rollback snapshot receipt")
    kind = snapshot.get("format", "tar.gz")
    if not isinstance(kind, str) or kind not in {"tar.gz", "directory-v1"}:
        raise OpsError("unsupported rollback snapshot format")
    suffix = ".snapshot" if kind == "directory-v1" else ".tar.gz"
    return backup_root(c) / (valid_id(snapshot["backup_id"]) + suffix)

def safe_extract(bundle: Path, target: Path, *, max_bytes: int = 200 * 1024**3) -> None:
    total = 0; seen = set()
    with tarfile.open(bundle, "r:gz") as tar:
        for entry in tar:
            name = PurePosixPath(entry.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or str(name) in seen:
                raise OpsError("invalid or duplicate archive path")
            seen.add(str(name))
            if not (entry.isfile() or entry.isdir()): raise OpsError("archive links and special files are forbidden")
            total += entry.size
            if total > max_bytes or len(seen) > 1_000_000: raise OpsError("archive exceeds restore limits")
            dest = target.joinpath(*name.parts)
            if entry.isdir(): dest.mkdir(parents=True, mode=0o700, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                stream = tar.extractfile(entry)
                if stream is None: raise OpsError("archive member missing")
                with dest.open("xb") as out: shutil.copyfileobj(stream, out)
                dest.chmod(0o600)

def verify_snapshot(stage: Path) -> dict:
    if stage.is_symlink() or not stage.is_dir(): raise OpsError("invalid snapshot directory")
    actual, directories = set(), set()
    for p, info in runtime_entries(stage):
        name = str(p.relative_to(stage))
        if stat.S_ISDIR(info.st_mode): directories.add(name)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1: actual.add(name)
        else: raise OpsError("backup contains a link or special file")
    manifest_path = stage / "backup.json"
    if "backup.json" not in actual or manifest_path.stat().st_size > 64 * 1024**2:
        raise OpsError("invalid backup manifest")
    try: m = json.loads(manifest_path.read_text())
    except (ValueError, UnicodeError): raise OpsError("invalid backup manifest") from None
    if not isinstance(m, dict) or m.get("version") != 1 or not isinstance(m.get("files"), dict):
        raise OpsError("invalid backup manifest")
    actual.remove("backup.json")
    if not set(BACKUP_DIRS) <= directories:
        raise OpsError("backup directory set mismatch")
    for name in directories:
        if PurePosixPath(name).parts[0] not in BACKUP_DIRS: raise OpsError("backup path outside data set")
    if m.get("format") == "directory-v1" and (
            not isinstance(m.get("directories"), list) or
            any(not isinstance(name, str) for name in m["directories"]) or
            sorted(directories) != sorted(m["directories"])):
        raise OpsError("backup directory set mismatch")
    if actual != set(m["files"]): raise OpsError("backup file set mismatch")
    for name, expected in m["files"].items():
        rel = PurePosixPath(name)
        if (not rel.parts or str(rel) != name or rel.is_absolute() or ".." in rel.parts or
                len(rel.parts) < 2 or rel.parts[0] not in BACKUP_DIRS):
            raise OpsError("backup path outside data set")
        if (not isinstance(expected, dict) or type(expected.get("size")) is not int or expected["size"] < 0 or
                not isinstance(expected.get("sha256"), str) or not DIGEST.fullmatch(expected["sha256"])):
            raise OpsError("invalid backup file manifest")
        p = stage / name
        if p.stat().st_size != expected["size"] or digest(p) != expected["sha256"]: raise OpsError("backup checksum mismatch")
        if not p.name.endswith(("-wal", "-shm")): sqlite_check(p)
    return m

@contextmanager
def secret_transaction(c: dict):
    """Restore prior credential files if a multi-file update is interrupted."""
    d = Path(c["data_root"])
    paths = [d / "private" / n for n in c["secret_arns"] if n not in {"hermes.env", "hermes.yaml"}]
    paths += [d / "hermes" / ".env", d / "hermes" / "config.yaml", d / "materialized-secrets.json"]
    paths += [d / "private" / name for name in ("mail-inference.json", "openrouter-api-key")]
    saved = {}
    for p in paths:
        if p.is_symlink(): raise OpsError("secret path is a symlink")
        saved[p] = (p.read_bytes(), p.stat()) if p.exists() else None
    try:
        yield
    except Exception:
        for p, previous in saved.items():
            if previous is None:
                p.unlink(missing_ok=True)
                continue
            contents, info = previous
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(dir=p.parent, prefix=".secret-recover-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(contents); stream.flush(); os.fsync(stream.fileno())
                os.chown(temporary, info.st_uid, info.st_gid)
                os.chmod(temporary, stat.S_IMODE(info.st_mode))
                os.replace(temporary, p)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        raise


def materialize_secrets(c: dict, versions: dict | None = None) -> None:
    with secret_transaction(c):
        _materialize_secrets(c, versions)


def mail_secret_projections(values: dict[str, str]) -> dict[str, str]:
    """Derive narrowly mounted mail files from pinned existing secret versions.

    The profile lives in config.json; only OPENROUTER_API_KEY is copied from
    hermes.env. No shell evaluation, extra AWS authority, or Telegram exposure.
    """
    try:
        runtime = json.loads(values.get("config.json", "{}"))
    except ValueError:
        return {}  # Runtime validation remains authoritative for legacy config.
    if not isinstance(runtime, dict) or runtime.get("mail_inference_profile") is None:
        return {}
    profile = runtime["mail_inference_profile"]
    generation = profile.get("structured_generation") if isinstance(profile, dict) else None
    if (runtime.get("mail_inference_config") != "/run/job-search/mail-inference.json"
            or not isinstance(generation, dict) or generation.get("kind") != "openrouter"
            or generation.get("credential_file") != "/run/job-search/openrouter-api-key"
            or profile.get("embeddings") is not None):
        raise OpsError("invalid dedicated OpenRouter mail profile")
    matches = re.findall(r"^[ \t]*(?:export[ \t]+)?OPENROUTER_API_KEY[ \t]*=[ \t]*([^\r\n]*)\r?$", values.get("hermes.env", ""), re.MULTILINE)
    try:
        tokens = shlex.split(matches[0], comments=True) if len(matches) == 1 else []
    except ValueError:
        tokens = []
    if len(tokens) != 1 or not tokens[0] or len(tokens[0]) > 4096 or any(ch.isspace() or ord(ch) < 32 for ch in tokens[0]):
        raise OpsError("dedicated mail requires one valid OpenRouter credential")
    return {"mail-inference.json": json.dumps(profile) + "\n", "openrouter-api-key": tokens[0] + "\n"}


def _materialize_secrets(c: dict, versions: dict | None = None) -> None:
    d = Path(c["data_root"])
    sources = versions if versions is not None else {n: {"arn": arn} for n, arn in c["secret_arns"].items()}
    if set(sources) != set(c["secret_arns"]): raise OpsError("restore secret inventory mismatch")
    pending = []
    resolved = {}
    for name, ref in sources.items():
        if name not in SECRET_FILES or ref.get("arn") != c["secret_arns"].get(name): raise OpsError("unrecognized restore secret")
        argv = ["secretsmanager", "get-secret-value", "--secret-id", ref["arn"]]
        if ref.get("version_id"): argv += ["--version-id", ref["version_id"]]
        response = json.loads(aws(c, *argv))
        value = response.get("SecretString")
        if not isinstance(value, str) or not value or len(value) > 65536: raise OpsError("secret is empty or invalid")
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id or (ref.get("version_id") and version_id != ref["version_id"]):
            raise OpsError("secret version mismatch")
        resolved[name] = {"arn": ref["arn"], "version_id": version_id}
        target = d / "private" / name
        if name == "hermes.env": target = d / "hermes" / ".env"
        if name == "hermes.yaml": target = d / "hermes" / "config.yaml"
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The portable key must never be silently rotated under existing ciphertext.
        if name == "portable-master-key" and target.exists() and target.read_text() != value:
            raise OpsError("portable encryption key changed; explicit state migration required")
        pending.append((name, target, value))
    projections = mail_secret_projections({name: value for name, _target, value in pending})
    for name, value in projections.items():
        target = d / "private" / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        pending.append((name, target, value))
    for name, target, value in pending:
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".secret-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(value); stream.flush(); os.fsync(stream.fileno())
            os.chown(temp, 0 if name == "tailscale-auth-key" else c.get("app_uid", 10001), 0 if name == "tailscale-auth-key" else c.get("app_gid", 10001))
            os.replace(temp, target)
            sync_directory(target.parent)
        finally:
            if os.path.exists(temp): os.unlink(temp)
    write_json(d / "materialized-secrets.json", resolved)


def point_current(c: dict, path: Path | None) -> None:
    root = Path(c["release_root"]); temporary = root / ".next-current"
    if path is None:
        current = root / "current"
        if current.exists() and not current.is_symlink():
            raise OpsError("current release pointer is not a symlink")
        current.unlink(missing_ok=True)
        sync_directory(root)
        return
    if temporary.exists() or temporary.is_symlink(): temporary.unlink()
    temporary.symlink_to(path)
    os.replace(temporary, root / "current")
    sync_directory(root)

def chown_runtime(c: dict) -> None:
    d = Path(c["data_root"])
    # Create bind sources before Docker can create root-owned 0755 directories.
    # Both socket servers require an owner-only parent directory.
    for name in ("state", "private", "hermes", "runtime", "runtime/tools", "runtime/notifications"):
        root = d / name; root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink(): raise OpsError("runtime contains symlink")
        if name.startswith("runtime/"): root.chmod(0o700)
        for p, info in [(root, root.lstat()), *runtime_entries(root, hermes=name == "hermes")]:
            if stat.S_ISLNK(info.st_mode): raise OpsError("runtime contains symlink")
            if p.name == "tailscale-auth-key": continue
            relative = p.relative_to(root).parts
            if relative and ((name == "state" and relative[0] == "review-runner") or
                             (name == "runtime" and relative[0] == "reviews")):
                os.chown(p, 0, 0)
                p.chmod(0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
                continue
            if p == root and name == "private":
                os.chown(p, 0, c.get("app_gid", 10001)); p.chmod(0o710)
                continue
            os.chown(p, c.get("app_uid", 10001), c.get("app_gid", 10001))
    # Toolchain is immutable to application UID, but readable/executable for tools.
    toolchain = d / "toolchain"; toolchain.mkdir(mode=0o755, exist_ok=True); toolchain.chmod(0o755)
    for p in toolchain.iterdir():
        if p.is_symlink(): raise OpsError("toolchain symlink forbidden")
        os.chown(p, 0, 0); p.chmod(0o555 if p.name == "tectonic" else 0o444)


def used_secret_versions(c: dict) -> dict:
    return secret_versions(c) if c["secret_arns"] else {}


def undo_restore(c: dict, op: Operation) -> None:
    info = op.value.get("restore")
    if not info or info.get("committed"):
        return
    root = Path(c["data_root"])
    saved = op.root / (op.id + "-saved")
    rejected = op.root / (op.id + "-rejected")
    rejected.mkdir(mode=0o700, exist_ok=True)
    for name, original_inode in info["original_inodes"].items():
        if name not in BACKUP_DIRS:
            raise OpsError("invalid restore journal directory")
        live, old = root / name, saved / name
        if old.exists():
            if live.exists():
                if (rejected / name).exists():
                    raise OpsError("ambiguous restore directories; preserve for inspection")
                os.replace(live, rejected / name)
                sync_directory(root); sync_directory(rejected)
            os.replace(old, live)
            sync_directory(saved); sync_directory(root)
        elif original_inode is not None:
            if not live.is_dir() or live.stat().st_ino != original_inode:
                raise OpsError("original restore directory is missing; preserve for inspection")
        elif live.exists():
            os.replace(live, rejected / name)
            sync_directory(root); sync_directory(rejected)
    materialize_secrets(c, info["prior_secrets"])
    if info.get("prior_release"):
        point_current(c, release_path(c, info["prior_release"]))
    op.update("restore_reverted", restore=None)


def restore_unlocked(c: dict, bundle: Path, expected_sha256: str, *, replace: bool = False,
                     _operation: Operation | None = None) -> dict:
    if bundle.is_symlink(): raise OpsError("backup path is a symlink")
    directory_snapshot = bundle.is_dir()
    checksum_path = bundle / "backup.json" if directory_snapshot else bundle
    if (checksum_path.is_symlink() or not checksum_path.is_file() or
            not isinstance(expected_sha256, str) or not DIGEST.fullmatch(expected_sha256) or
            digest(checksum_path) != expected_sha256):
        raise OpsError("backup archive checksum mismatch")
    d = Path(c["data_root"])
    current = Path(c["release_root"]) / "current"
    if current.exists() and running_services(c):
        raise OpsError("stop every application service before restore")
    occupied = any((d / name).exists() and any((d / name).iterdir()) for name in BACKUP_DIRS)
    if occupied and not replace:
        raise OpsError("restore target is not empty; use explicit --replace")
    if directory_snapshot:
        source_manifest = verify_snapshot(bundle)
        if source_manifest.get("format") != "directory-v1": raise OpsError("unsupported rollback snapshot format")
        required = sum(item["size"] for item in source_manifest["files"].values())
    else:
        with tarfile.open(bundle, "r:gz") as archive:
            required = sum(member.size for member in archive)
    available_space(c, restore_bytes=required, local_snapshot=directory_snapshot)
    if _operation is None:
        require_idle(c)
    # Preserve staging on interruption. It is never part of a normal backup.
    stage = Path(tempfile.mkdtemp(prefix="restore-stage-", dir=d))
    stage.chmod(0o700)
    if directory_snapshot:
        # Recovery staging owns independent bytes: publication must never move
        # or hard-link the retained rollback snapshot, including on retries.
        shutil.copytree(bundle, stage, dirs_exist_ok=True)
        if digest(stage / "backup.json") != expected_sha256: raise OpsError("backup manifest changed during recovery")
    else: safe_extract(bundle, stage)
    m = verify_snapshot(stage)
    target_release = release_path(c, m["release_id"]); manifest(target_release)
    sync_tree(stage); sync_directory(d)
    op = _operation or Operation.begin(c, "restore", previous_release=release_path(c).name if current.exists() else None,
                                        target_release=target_release.name, active_services=[], previous_activation=activation(c))
    saved = op.root / (op.id + "-saved")
    if saved.exists() and any(saved.iterdir()):
        # A second restore after a recovered interruption gets a distinct saved set.
        os.replace(saved, op.root / (op.id + "-saved-" + os.urandom(4).hex()))
    saved.mkdir(mode=0o700, exist_ok=True)
    info = {"stage": stage.name, "committed": False,
            "original_inodes": {name: (d / name).stat().st_ino if (d / name).exists() else None for name in BACKUP_DIRS},
            "prior_release": release_path(c).name if current.exists() else None,
            "prior_secrets": used_secret_versions(c), "target_secrets": m["secrets"]}
    op.update("restore_prepared", restore=info)
    set_gate(c, [], draining=True)
    try:
        # Resolve every required version before moving a directory.
        materialize_secrets(c, m["secrets"])
        for name in BACKUP_DIRS:
            op.update("restore_publish_" + name)
            if (d / name).exists():
                os.replace(d / name, saved / name)
                sync_directory(d); sync_directory(saved)
            if (stage / name).exists():
                os.replace(stage / name, d / name)
                sync_directory(stage); sync_directory(d)
        materialize_secrets(c, m["secrets"])
        chown_runtime(c); point_current(c, target_release)
        write_json(d / "activation.json", {"enabled": False, "reason": "restored; verify before resuming external actions"})
        op.update("restore_committed", restore={**info, "committed": True})
    except Exception:
        undo_restore(c, op)
        if _operation is None:
            op.finish("restore_aborted")
        raise
    if _operation is None:
        op.finish("restored_paused")
    return {"status": "restored_paused", "operation_id": op.id, "backup_id": m["backup_id"], "release_id": m["release_id"]}


def project_containers() -> list[str]:
    # Include one-shot initialization containers, even with a broken current link.
    query = ["docker", "ps", "--filter", "label=com.docker.compose.project=job-search", "--format", "{{.ID}}"]
    ids = run(query).split()
    if any(not re.fullmatch(r"[a-f0-9]{12,64}", value) for value in ids):
        raise OpsError("invalid project container identity")
    from .review_host import worker_containers
    return sorted(set(ids + worker_containers(run)))


def stop_project(c: dict) -> None:
    set_gate(c, [], draining=True)
    from .review_host import drain as drain_reviews
    drain_reviews(c, run)
    ids = project_containers()
    if ids:
        run(["docker", "stop", "--time", "4200", *ids], timeout=4500)
    if project_containers():
        raise OpsError("project still has writers; recovery cannot proceed")


def recover(c: dict, operation_id: str) -> dict:
    value = read_operation(c)
    if not value or value["operation_id"] != operation_id:
        raise OpsError("operation ID does not match current recovery record")
    if value["complete"]:
        return {"status": "already_complete", "operation_id": operation_id, "phase": value["phase"]}
    op = Operation(c, value)
    set_gate(c, [], draining=True)
    stop_project(c)
    if value.get("restore") and not value["restore"].get("committed"):
        undo_restore(c, op)
    if value["kind"] in {"deploy", "rollback"} and not value["writes_possible"]:
        snapshot = value.get("backup")
        if snapshot:
            restore_unlocked(c, local_backup_path(c, snapshot),
                             snapshot["sha256"], replace=True, _operation=op)
        elif value.get("state_mutation_started"):
            raise OpsError("operation has no verified rollback snapshot; preserve data for inspection")
        elif value.get("previous_release"):
            point_current(c, release_path(c, value["previous_release"]))
        if not value.get("previous_release"):
            point_current(c, None)
    elif value["kind"] == "restore" and (value.get("restore") or {}).get("committed"):
        materialize_secrets(c, value["restore"]["target_secrets"])
        point_current(c, release_path(c, value["target_release"]))
    write_json(Path(c["data_root"]) / "activation.json", {"enabled": False, "reason": "operation recovered; verify before activation"})
    op.finish("recovered_paused")
    return {"status": "recovered_paused", "operation_id": op.id, "data_preserved": True}


def healthy(c: dict, expected: list[str]) -> bool:
    raw = compose(c, "ps", "--format", "json")
    try:
        parsed = json.loads(raw)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
    indexed = {e.get("Service"): e for e in entries}
    return all(indexed.get(s, {}).get("State") == "running" and indexed[s].get("Health") == "healthy" for s in expected)


def wait_healthy(c: dict, expected: list[str], timeout: int = 180) -> None:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if healthy(c, expected): return
        time.sleep(3)
    raise OpsError("release health checks failed")


def deploy(c: dict, release_id: str, *, rollback: bool = False) -> dict:
    from .release_policy import check_transition
    require_idle(c)
    target = release_path(c, release_id); candidate = manifest(target)
    root = Path(c["release_root"]); current = root / "current"
    previous = release_path(c) if current.exists() else None
    if previous and previous.name == release_id:
        state = activation(c)
        active = configured_services(c) if state.get("enabled") else configured_services(c, review=True) if state.get("mode") == "review" else []
        if active and not healthy(c, active):
            raise OpsError("release already installed but unhealthy; inspect status before retrying")
        return {"status": "deployed" if active else "deployed_paused", "release_id": release_id, "already_installed": True}
    check_transition(candidate, manifest(previous) if previous else None, rollback=rollback)
    compose(c, "config", "--quiet", release=target)
    compose(c, "pull", release=target)
    if candidate.get("reviewer_image"):
        run(["docker", "pull", candidate["reviewer_image"]], timeout=1800)
    if preflight(c)["issues"]:
        raise OpsError("preflight is incomplete")
    available_space(c, local_snapshot=True)
    active = running_services(c) if previous else []
    prior_activation = activation(c)
    op = Operation.begin(c, "rollback" if rollback else "deploy", previous_release=previous.name if previous else None,
                         target_release=release_id, active_services=active, previous_activation=prior_activation,
                         backup=None, state_mutation_started=False,
                         recovery_tool=str(Path(__file__).resolve().parent.parent / "scripts" / "job-search-ops"))
    try:
        stop_for_maintenance(c, op, active)
        # Fresh seed state also needs a recoverable snapshot before initialization.
        op.update("snapshotting")
        snapshot = backup_unlocked(c, paused=True, upload=False, release_id=previous.name if previous else release_id)
        op.update("snapshotted", backup=snapshot)
        op.update("initializing", state_mutation_started=True)
        materialize_secrets(c); chown_runtime(c)
        from .review_host import prepare as prepare_reviewer
        prepare_reviewer(c, target, candidate, run)
        if preflight(c)["issues"]: raise OpsError("preflight is incomplete")
        point_current(c, target)
        set_gate(c, [], initialize=op.id)
        compose(c, "run", "--rm", "--no-deps", "--env", "JOB_SEARCH_INITIALIZE_OPERATION=" + op.id, "initialize")
        set_gate(c, [], draining=True)
        compose(c, "up", "-d", "--no-deps", "--no-build", "tools")
        wait_healthy(c, ["tools"])
        compose(c, "stop", "tools")
        if prior_activation.get("enabled"): model_readiness(c)
        write_json(Path(c["data_root"]) / "release-state.json", {"current": release_id,
                   "previous": previous.name if previous else None, "backup": op.value["backup"]})
    except Exception:
        if op.value["complete"]:  # Drain timed out before any data changed.
            raise
        set_gate(c, [], draining=True)
        stop_project(c)
        if op.value.get("backup"):
            point_current(c, previous or target)
            snapshot = op.value["backup"]
            restore_unlocked(c, local_backup_path(c, snapshot),
                             snapshot["sha256"], replace=True, _operation=op)
        elif op.value["state_mutation_started"]:
            op.update("recovery_required")
            raise
        point_current(c, previous)
        # Recovered old state is consistent; any subsequent writes forbid rewind.
        op.update("resuming_previous", writes_possible=True)
        set_gate(c, active)
        if active:
            compose(c, "up", "-d", "--no-deps", "--no-build", "--force-recreate", *active)
            wait_healthy(c, active)
        write_json(Path(c["data_root"]) / "activation.json", prior_activation)
        op.finish("deployment_reverted")
        raise
    try:
        op.update("resuming", writes_possible=bool(active))
        set_gate(c, active)
        if active:
            compose(c, "up", "-d", "--no-deps", "--no-build", "--force-recreate", *active)
            wait_healthy(c, active)
        write_json(Path(c["data_root"]) / "activation.json", prior_activation)
        op.finish("deployed" if active else "deployed_paused", downtime_finished_at=operation_now())
    except Exception:
        pause(c, reason="release failed after validation; data retained for inspection")
        op.update("recovery_required")
        raise
    retention = "retained_two_newest"
    try: prune_local_backups(c, keep_backup=op.value["backup"]["backup_id"])
    except (OSError, OpsError):
        # Deployment is already complete. Keep old evidence rather than turn a
        # cleanup failure into an ambiguous failed deployment or stop writers.
        retention = "cleanup_failed_backups_preserved"
    image_retention = {"status": "cleanup_failed"}
    try: image_retention = prune_local_images(c, current=target, previous=previous)
    except (OSError, OpsError, ValueError, KeyError, TypeError):
        # Image cleanup is best effort after deployment commits. An inventory,
        # registry or removal error must never pause or roll back healthy writers.
        pass
    return {"status": op.value["phase"], "release_id": release_id, "operation_id": op.id,
            "previous_release": op.value["previous_release"], "phase_times": op.value["phase_times"],
            "backup": op.value["backup"], "local_backup_retention": retention,
            "local_image_retention": image_retention}


def prune_local_images(c: dict, *, current: Path, previous: Path | None) -> dict:
    """Called only after successful deployment, with the operations lock held."""
    from .image_retention import prune
    require_idle(c)
    if release_path(c) != current:
        raise OpsError("current release changed before image retention")
    return prune(c, current=current, previous=previous, read_manifest=manifest,
                 run=run, aws=lambda *args: aws(c, *args))


def prune_local_backups(c: dict, *, keep_backup: str) -> None:
    """Retain the two newest published local backups, across both formats.

    Never touch interrupted staging, unusual paths, or the current journal's
    snapshot. Cleanup happens only after services have successfully resumed.
    """
    root = backup_root(c)
    candidates = [p for p in root.iterdir() if not p.is_symlink() and
                  re.fullmatch(r"[0-9]{8}T[0-9]{6}-[a-f0-9]{8}\.(?:snapshot|tar\.gz)", p.name) and
                  (p.is_dir() if p.name.endswith(".snapshot") else p.is_file())]
    candidates.sort(key=lambda p: p.name, reverse=True)
    for item in candidates[2:]:
        if item.name in {keep_backup + ".snapshot", keep_backup + ".tar.gz"}: continue
        if item.is_dir(): shutil.rmtree(item)
        else: item.unlink()
    sync_directory(root)


def preflight(c: dict) -> dict:
    issues = []
    try: verify_mount(c)
    except OpsError: issues.append("persistent_volume")
    for tool in ("aws", "docker", "tailscale"):
        if not shutil.which(tool): issues.append("missing_" + tool)
    d = Path(c["data_root"])
    files = [d / "private" / name for name in ("config.json", "inference.json", "resume-model.json", "mcp-token", "portable-master-key", "runpod-api-key")]
    files += [d / "hermes" / ".env", d / "hermes" / "config.yaml"]
    for path in files:
        if path.is_symlink() or not path.is_file(): issues.append("missing_" + path.name)
        elif path.stat().st_mode & 0o077 or path.stat().st_uid != c.get("app_uid", 10001):
            issues.append("unsafe_permissions_" + path.name)
    for name in ("tectonic", "tectonic.bundle"):
        if not (d / "toolchain" / name).is_file(): issues.append("missing_" + name)
    return {"status": "blocked_setup" if issues else "configuration_ready", "issues": issues}


def model_readiness(c: dict) -> None:
    # Run the target application's existing offline checks in its exact image.
    # No paid inference, worker tick, mailbox synchronization, or network probe.
    probe = "\n".join([
        "import json",
        "from pathlib import Path",
        "from job_search.runtime import load_runtime_config",
        "from job_search.cli import _dependency_health",
        "c = load_runtime_config(Path('/run/job-search/config.json'))",
        "r = _dependency_health(c)['inference']",
        "print(json.dumps({'configured': r.get('configuration_ready', False), 'embedding': r.get('preference_embeddings', {}).get('status'), 'ranking_refresh_mode': c.ranking_refresh_mode, 'shortlist_policy': c.shortlist_policy, 'mail_ready': not c.remote_mail_inference_enabled or r.get('remote_mail', {}).get('status') in ('configuration_ready', 'local_classifier_selected'), 'private_dashboard': bool(c.dashboard_https_origin and c.dashboard_allowed_tailscale_login)}))",
    ])
    report = json.loads(compose(c, "run", "--rm", "--no-deps", "--entrypoint", "python", "core", "-c", probe))
    sparse_ready = (report.get("ranking_refresh_mode") == "sparse_cpu"
                    and report.get("shortlist_policy") in {"broad", "selective", "compare"}
                    and report.get("embedding") == "not_required")
    if not report.get("configured") or not (report.get("embedding") == "ready" or sparse_ready):
        raise OpsError("remote inference or ranking model migration is incomplete")
    if not report.get("private_dashboard"): raise OpsError("private dashboard owner is not configured")
    if report.get("mail_ready") is False: raise OpsError("remote mail inference is not ready")


def pause(c: dict, *, reason: str = "operator paused") -> dict:
    set_gate(c, [], draining=True)
    # Record intent before draining. Status separately detects unexpected runners.
    write_json(Path(c["data_root"]) / "activation.json", {"enabled": False, "reason": reason})
    from .review_host import drain as drain_reviews
    drain_reviews(c, run)
    compose(c, "stop", "--timeout", "4200", *configured_services(c))
    if running_services(c): raise OpsError("services failed to stop")
    return {"status": "paused"}


def activate(c: dict) -> dict:
    require_idle(c)
    check = preflight(c)
    if check["issues"]: raise OpsError("preflight is incomplete")
    model_readiness(c)
    set_gate(c, configured_services(c))
    try:
        compose(c, "up", "-d", "--no-deps", "--no-build", "--force-recreate", *configured_services(c))
        wait_healthy(c, configured_services(c))
    except Exception:
        pause(c, reason="activation failed; investigate before retrying")
        raise
    write_json(Path(c["data_root"]) / "activation.json", {"enabled": True})
    return {"status": "active"}


def review(c: dict) -> dict:
    """Enroll and check interactive services with recurring workers stopped."""
    if preflight(c)["issues"]: raise OpsError("preflight is incomplete")
    require_idle(c)
    pause(c, reason="interactive setup")
    set_gate(c, configured_services(c, review=True))
    try:
        compose(c, "up", "-d", "--no-deps", "--no-build", "--force-recreate", *configured_services(c, review=True))
        wait_healthy(c, configured_services(c, review=True))
    except Exception:
        pause(c, reason="interactive setup failed")
        raise
    write_json(Path(c["data_root"]) / "activation.json", {"enabled": False, "mode": "review"})
    return {"status": "review", "recurring_workers_enabled": False}


def domain_readiness(c: dict) -> dict:
    """Query the active release's read-only report; never start an idle worker."""
    # Bound the process inside Docker too: killing the host docker client alone
    # leaves its expensive readiness query running in the container.
    runner = "import subprocess,sys; subprocess.run(sys.argv[1:],check=True,timeout=40)"
    raw = compose(c, "exec", "-T", "core", "python", "-c", runner, "python", "-m", "job_search",
                  "--config", "/run/job-search/config.json", "readiness", "--monitor")
    if len(raw.encode("utf-8")) > 128 * 1024:
        raise OpsError("domain readiness response is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise OpsError("domain readiness response is invalid")
    if value.get("status") not in {"disabled", "paused", "configured_unverified", "ready", "stale", "blocked"}:
        raise OpsError("domain readiness response status is invalid")
    metrics = value.get("metrics")
    if not isinstance(metrics, dict):
        raise OpsError("domain readiness metrics are invalid")
    for name in ("stale_capabilities", "unresolved_work", "pending_reconciliation"):
        count = metrics.get(name)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count < 10**12:
            raise OpsError("domain readiness metric is invalid")
    # Publish only status/counts; no dependency payload or private error text.
    return {"schema_version": 1, "status": value["status"], "metrics": metrics}


def status(c: dict, publish: bool = False) -> dict:
    # Leave time inside systemd's 240s limit to publish failures even if Docker
    # or a readiness query stalls. The deadline applies only to this diagnostic.
    token = _STATUS_DEADLINE.set(time.monotonic() + 150)
    try:
        return _status(c, publish)
    finally:
        _STATUS_DEADLINE.reset(token)


def _status(c: dict, publish: bool = False) -> dict:
    result = preflight(c)
    from .review_host import status as reviewer_status
    result["codex_reviews"] = reviewer_status(c)
    # This bounded local snapshot adds no CloudWatch dimensions or metrics.
    # Host counters describe current pressure; they cannot explain past peaks.
    try:
        from .resource_usage import memory_snapshot
        result["host_memory"] = {"measured_at": datetime.now(timezone.utc).isoformat(),
                                 "counters": {key: value for key, value in memory_snapshot().items()
                                              if key.startswith("host_")}}
    except Exception:
        result["host_memory"] = {"status": "unavailable"}
    ok = False
    enabled = False
    try:
        current = release_path(c); result["release_id"] = current.name
        activation = json.loads((Path(c["data_root"]) / "activation.json").read_text())
        enabled = activation.get("enabled", False)
        result["automation_enabled"] = enabled
        ok = healthy(c, configured_services(c)) and not result["issues"] if enabled else False
        result["status"] = "healthy" if ok else ("attention" if enabled else "paused")
        if not enabled:
            active = running_services(c)
            if activation.get("mode") == "review" and set(active) == set(configured_services(c, review=True)) and healthy(c, configured_services(c, review=True)):
                result["status"] = "review"
            elif active: result["status"] = "attention"
    except (OSError, ValueError, OpsError): result["status"] = "blocked_setup"
    domain = {"schema_version": 1, "status": "paused" if not enabled else "blocked",
              "metrics": {"stale_capabilities": 0, "unresolved_work": 0, "pending_reconciliation": 0}}
    if enabled:
        try:
            domain = domain_readiness(c)
        except (OSError, ValueError, OpsError):
            domain["reason_code"] = "domain_report_unavailable"
    result["domain"] = domain
    domain_ok = not enabled or domain["status"] in {"ready", "configured_unverified", "disabled", "paused"}
    # Preserve the independent process/container signal used during deployment.
    result["liveness_healthy"] = ok
    if enabled and not domain_ok and result["status"] == "healthy":
        result["status"] = "attention"
    age = -1.0
    try:
        receipt = json.loads((Path(c["data_root"]) / "last-backup.json").read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(receipt["created_at"])).total_seconds() / 3600
    except (OSError, ValueError, KeyError): pass
    result["backup_age_hours"] = round(age, 2)
    result["backup_overdue"] = age < 0 or age > 24
    attempt_path = Path(c["data_root"]) / "backup-attempt.json"
    try:
        attempt = json.loads(attempt_path.read_text())
        in_progress = bool(attempt.get("in_progress"))
        result["backup_attempt_in_progress"] = in_progress and operation_lock_held(c) is True
        result["backup_attempt_failed"] = bool(attempt.get("failed")) or (in_progress and not result["backup_attempt_in_progress"])
    except (OSError, ValueError):
        result["backup_attempt_failed"] = False
        result["backup_attempt_in_progress"] = False
    operation = read_operation(c)
    result["operation"] = {key: operation.get(key) for key in ("operation_id", "kind", "phase", "complete", "writes_possible", "previous_release", "target_release", "started_at", "updated_at", "phase_times", "recovery_tool", "backup")} if operation else None
    incomplete = bool(operation and not operation["complete"])
    held = operation_lock_held(c) if incomplete else False
    result["maintenance_active"] = incomplete and held is True
    result["recovery_required"] = incomplete and held is not True
    if result["maintenance_active"]:
        result["status"] = "maintenance"
        result["next_action"] = "wait_for_active_operation"
    if result["recovery_required"]:
        result["status"] = "attention"
        result["next_action"] = "recover --operation " + operation["operation_id"]
        result["recovery_tool"] = operation.get("recovery_tool")
        if held is None: result["operation_lock_unverified"] = True
    grace = False
    if result["maintenance_active"] and operation["kind"] == "backup":
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(operation["started_at"])).total_seconds()
            grace = 0 <= elapsed < MAINTENANCE_GRACE_SECONDS
        except (KeyError, TypeError, ValueError):
            pass
    result["maintenance_alarm_grace"] = grace
    if publish:
        # Diagnostic failures must not consume the publication budget.
        _STATUS_DEADLINE.set(time.monotonic() + 30)
        dimensions = [{"Name": "InstanceId", "Value": c["instance_id"]}]
        values = {
            "Healthy": int(ok), "MaintenanceActive": int(grace),
            "BackupAttemptFailed": int(result["backup_attempt_failed"]),
            "BackupAgeSeconds": age * 3600 if age >= 0 else 999999,
            "MonitorHeartbeat": 1, "DomainReady": int(domain_ok),
            "DomainStaleCapabilities": domain["metrics"]["stale_capabilities"],
            "DomainUnresolvedWork": domain["metrics"]["unresolved_work"],
            "DomainPendingReconciliation": domain["metrics"]["pending_reconciliation"],
        }
        aws(c, "cloudwatch", "put-metric-data", "--namespace", c.get("cloudwatch_namespace", "CareerPlatform"),
            "--metric-data", json.dumps([
                {"MetricName": name, "Value": value, "Unit": "Seconds" if name == "BackupAgeSeconds" else "Count", "Dimensions": dimensions}
                for name, value in values.items()
            ]))
    return result


def scheduled_backup(c: dict, *, now: datetime | None = None) -> dict:
    stamp = now or datetime.now(timezone.utc)
    # Twice daily leaves room for copying/upload/retry before the 24-hour RPO.
    window = stamp.replace(hour=20 if stamp.hour >= 20 else 8, minute=0, second=0, microsecond=0)
    if stamp < window:
        window -= timedelta(hours=12)
    path = Path(c["data_root"]) / "backup-attempt.json"
    attempt = json.loads(path.read_text()) if path.exists() else {}
    if attempt.get("window") != window.isoformat():
        attempt = {"window": window.isoformat(), "attempts": 0, "succeeded": False,
                   "failed": bool(attempt.get("failed") or attempt.get("in_progress"))}
    if attempt["succeeded"] or attempt["attempts"] >= 4:
        return {"status": "scheduled_wait", "attempts": attempt["attempts"]}
    if attempt.get("retry_at") and stamp < datetime.fromisoformat(attempt["retry_at"]):
        return {"status": "scheduled_wait", "attempts": attempt["attempts"]}
    attempt.update(attempts=attempt["attempts"] + 1, attempted_at=stamp.isoformat(),
                   retry_at=(stamp + timedelta(minutes=30)).isoformat(),
                   in_progress=True, failed=bool(attempt.get("failed")))
    write_json(path, attempt)
    try:
        result = backup_unlocked(c)
    except Exception:
        attempt.update(in_progress=False, failed=True)
        write_json(path, attempt)
        if attempt["attempts"] == 1 and c.get("notification_topic_arn"):
            try:
                aws(c, "sns", "publish", "--topic-arn", c["notification_topic_arn"],
                    "--subject", "Career Platform backup failed", "--message",
                    "Backup failed. Inspect host operations status. The previous successful backup is retained; scheduled retries require no unresolved maintenance operation.")
            except OpsError:
                pass  # Persistent failure metric remains the independent fallback.
        raise
    attempt.update(succeeded=True, failed=False, in_progress=False)
    write_json(path, attempt)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/job-search/operations.json"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("preflight")
    s = sub.add_parser("status"); s.add_argument("--publish", action="store_true")
    b = sub.add_parser("backup"); b.add_argument("--scheduled", action="store_true")
    recovery = sub.add_parser("recover"); recovery.add_argument("--operation", required=True)
    d = sub.add_parser("deploy"); d.add_argument("--release", required=True)
    r = sub.add_parser("restore"); r.add_argument("--bundle", type=Path, required=True); r.add_argument("--sha256", required=True); r.add_argument("--replace", action="store_true")
    rb = sub.add_parser("rollback"); rb.add_argument("--release", required=True)
    sub.add_parser("activate"); sub.add_parser("pause"); sub.add_parser("secrets"); sub.add_parser("review")
    args = parser.parse_args(argv)
    try:
        c = load_config(args.config)
        if args.action == "preflight": result = preflight(c)
        elif args.action == "status": result = status(c, args.publish)
        else:
            if os.geteuid() != 0: raise OpsError("mutating host operations require root")
            with lock(c):
                if args.action != "recover": require_idle(c)
                if args.action == "recover": result = recover(c, args.operation)
                elif args.action == "backup": result = scheduled_backup(c) if args.scheduled else backup_unlocked(c)
                elif args.action in {"deploy", "rollback"}:
                    result = deploy(c, args.release, rollback=args.action == "rollback")
                elif args.action == "restore":
                    from .review_host import runner_busy
                    if project_containers() or runner_busy(c): raise OpsError("stop all project containers and review coordinators before restore")
                    result = restore_unlocked(c, args.bundle, args.sha256, replace=args.replace)
                elif args.action == "secrets":
                    if (Path(c["release_root"]) / "current").exists() and running_services(c):
                        raise OpsError("pause before changing secrets; activate recreates mounts")
                    set_gate(c, [], draining=True)
                    from .review_host import drain as drain_reviews
                    drain_reviews(c, run)
                    materialize_secrets(c); chown_runtime(c); result = {"status": "secrets_materialized"}
                elif args.action == "pause": result = pause(c)
                elif args.action == "activate": result = activate(c)
                elif args.action == "review": result = review(c)
        print(json.dumps(result, sort_keys=True))
        # Publication success is distinct from application health. Unhealthy
        # metrics/JSON still raise the appropriate CloudWatch alarms.
        if args.action == "status" and args.publish and result["status"] != "blocked_setup":
            return 0
        return 0 if result["status"] not in {"attention", "blocked_setup"} else 2
    except OperationBusy as exc:
        if args.action == "backup" and args.scheduled:
            print(json.dumps({"status": "deferred", "reason": "maintenance_active"}))
            return 0
        print(json.dumps({"status": "error", "reason": str(exc)}), file=sys.stderr)
        return 2
    except (OpsError, OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError) as exc:
        # No credentials, subprocess output, or file contents in error output.
        reason = str(exc) if isinstance(exc, OpsError) else type(exc).__name__
        print(json.dumps({"status": "error", "reason": reason}), file=sys.stderr)
        return 2

if __name__ == "__main__": raise SystemExit(main())
