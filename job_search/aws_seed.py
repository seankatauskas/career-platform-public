"""Private, selective seed bundles for a fresh AWS installation.

This is not a database backup. It carries career facts, the explicitly selected
standard resume, and selected rankers with their teacher training/audit records.
It never loads model pickle files, copies the jobs corpus, restores a champion,
or transfers mailbox state, OAuth credentials, application history, or queues.
Run ``python -m job_search.aws_seed --help`` for the offline CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from contextlib import contextmanager
from typing import Any

from .resume_lab.career_store import CareerStore
from .resume_lab.contracts import StandardVersionInput, claims_from_json
from .resume_lab.service import ResumeLabService
from .resume_lab.artifacts import ResumePdfArtifactRepository, ArtifactNamespace


VERSION = 1
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_FILES = 100
STANDARD_FILES = (
    "Sean_Katauskas_Resume.pdf", "Sean_Katauskas_Resume.tex",
    "Sean_Katauskas_Resume.txt", "resume-content.json", "resume-provenance.json",
)
PROXY_TABLES = (
    "proxy_profiles", "proxy_runs", "proxy_queue", "proxy_predictions",
    "proxy_students", "proxy_student_audits",
)


class SeedError(ValueError):
    """A seed failed validation; no live destination should be modified."""


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or str(path) != name or "\\" in name
            or any(part in {".", ".."} for part in path.parts)):
        raise SeedError("unsafe bundle path")
    return name


def _source(path: Path, *, private: bool = False) -> Path:
    path = Path(path).expanduser().absolute()
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_mode & (0o077 if private else 0o022)):
        raise SeedError("source must be an owned regular file with safe permissions")
    # Symlinked ancestors make no-clobber and ownership checks ambiguous.
    if path != path.resolve():
        raise SeedError("source paths must not contain symlinks")
    return path


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def _read_db(path: Path, *, offline: bool = False):
    path = _source(path)
    if offline and any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise SeedError("offline sources must be stopped and checkpointed; WAL/journal exists")
    suffix = "?mode=ro&immutable=1" if offline else "?mode=ro"
    connection = sqlite3.connect(path.as_uri() + suffix, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _rows(connection, sql, parameters=()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters)]


def _profile_snapshot(path: Path, *, offline: bool) -> dict[str, Any]:
    # CareerStore initializes its schema. Use SQLite's snapshot/backup into an
    # isolated file rather than constructing a store over the source database.
    with tempfile.TemporaryDirectory(prefix="career-seed-read-") as directory:
        snapshot = Path(directory) / "career.db"
        _private_write(snapshot, b"")
        with _read_db(path, offline=offline) as source:
            with sqlite3.connect(snapshot) as destination:
                source.backup(destination)
        profile = CareerStore(snapshot).get_profile()
        current = profile["draft"] or profile["approved"]
        if current is None:
            raise SeedError("source has no career profile")
        return {"current": current, "source_approved_revision_id": profile["approved_revision_id"]}


def export_seed(*, career_db: Path, standard_dir: Path, preference_db: Path,
                proxy_db: Path, model_root: Path, run_ids: list[str], output: Path,
                offline_sources: bool = False) -> dict[str, Any]:
    """Create one owner-only tar, without unpickling models or changing sources."""
    if not run_ids or len(set(run_ids)) != len(run_ids):
        raise SeedError("select distinct model run IDs")
    for run_id in run_ids:
        if not re.fullmatch(r"run_[A-Za-z0-9_]+", run_id):
            raise SeedError("invalid model run ID")
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise SeedError("archive destination already exists")
    if not output.parent.is_dir() or output.parent != output.parent.resolve():
        raise SeedError("archive parent must be an existing non-symlink directory")
    files: dict[str, bytes] = {"career.json": _json(_profile_snapshot(career_db, offline=offline_sources))}
    for filename in STANDARD_FILES:
        files["standard-resume/" + filename] = _source(standard_dir / filename).read_bytes()
    # Preserve registration only when it already exists for the exact selected
    # TeX. Merely owning a PDF must not manufacture an approved/active standard.
    with _read_db(career_db, offline=offline_sources) as con:
        registered = _rows(con, """SELECT s.name,s.manual_rank,s.active,v.*
            FROM resume_standards s JOIN resume_standard_versions v
            ON v.version_id=s.active_version_id WHERE v.tex_source=?""",
            (files["standard-resume/Sean_Katauskas_Resume.tex"].decode(),))
    if len(registered) > 1:
        raise SeedError("selected standard matches multiple registrations")
    files["registration.json"] = _json(registered[0] if registered else None)
    placeholders = ",".join("?" for _ in run_ids)
    with _read_db(preference_db, offline=offline_sources) as con:
        registry = _rows(con, f"SELECT * FROM preference_model_runs WHERE run_id IN ({placeholders}) ORDER BY run_id", run_ids)
    if len(registry) != len(run_ids):
        raise SeedError("selected model run is missing from registry")
    for row in registry:
        run_dir = model_root / "runs" / row["run_id"]
        raw = _source(run_dir / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if manifest != json.loads(row["manifest_json"]) or manifest["run_id"] != row["run_id"]:
            raise SeedError("model manifest differs from registry")
        prefix = ".models/preference/runs/" + row["run_id"] + "/"
        files[prefix + "manifest.json"] = raw
        for name, expected in manifest["artifacts"].items():
            if _safe_name(name) != PurePosixPath(name).name:
                raise SeedError("model artifact must be a filename")
            raw = _source(run_dir / name).read_bytes()
            if _sha(raw) != expected:
                raise SeedError("model artifact checksum mismatch")
            files[prefix + name] = raw
        row["artifact_path"] = prefix.rstrip("/")
    with _read_db(proxy_db, offline=offline_sources) as con:
        students = _rows(con, f"SELECT * FROM proxy_students WHERE model_run_id IN ({placeholders}) ORDER BY policy_id", run_ids)
        if {row["model_run_id"] for row in students} != set(run_ids):
            raise SeedError("selected rankers lack teacher provenance")
        teacher_runs = sorted({row["run_id"] for row in students})
        rp = ",".join("?" for _ in teacher_runs)
        support = {
            "proxy_runs": _rows(con, f"SELECT * FROM proxy_runs WHERE run_id IN ({rp})", teacher_runs),
            "proxy_profiles": _rows(con, f"SELECT * FROM proxy_profiles WHERE profile_fingerprint IN (SELECT profile_fingerprint FROM proxy_runs WHERE run_id IN ({rp}))", teacher_runs),
            "proxy_queue": _rows(con, f"SELECT * FROM proxy_queue WHERE run_id IN ({rp}) ORDER BY queue_id", teacher_runs),
            "proxy_predictions": _rows(con, f"SELECT * FROM proxy_predictions WHERE queue_id IN (SELECT queue_id FROM proxy_queue WHERE run_id IN ({rp}))", teacher_runs),
            "proxy_students": students,
            "proxy_student_audits": _rows(con, f"SELECT * FROM proxy_student_audits WHERE model_run_id IN ({placeholders})", run_ids),
        }
    predicted = {row["queue_id"] for row in support["proxy_predictions"]}
    for row in support["proxy_queue"]:
        if row["status"] != "complete" or row["queue_id"] not in predicted or not row["semantic_text"]:
            raise SeedError("teacher training/audit snapshot is incomplete")
    for row in students:
        row["state_db"] = "job-boards-preference.db"
        row["artifact_dir"] = ".models/preference"
    files["model-support.json"] = _json({"registry": registry, "teacher": support})
    manifest = {"version": VERSION, "files": {name: {"sha256": _sha(raw), "size": len(raw)} for name, raw in files.items()}}
    files["manifest.json"] = _json(manifest)
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_ARCHIVE_BYTES:
        raise SeedError("seed exceeds archive limits")
    fd, temporary = tempfile.mkstemp(prefix=".seed-", dir=output.parent)
    os.close(fd)
    try:
        with tarfile.open(temporary, "w") as archive:
            for name, raw in sorted(files.items()):
                item = tarfile.TarInfo(name)
                item.mode, item.size = 0o600, len(raw)
                archive.addfile(item, io.BytesIO(raw))
        os.link(temporary, output)  # Atomic no-clobber publication.
    finally:
        os.unlink(temporary)
    return {"status": "exported", "sha256": _sha(output.read_bytes()),
            "model_runs": run_ids, "standard_registered": bool(registered),
            "career_approval": "pending_user_review"}


def _read_archive(archive: Path, expected_sha256: str) -> dict[str, bytes]:
    path = _source(archive, private=True)
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise SeedError("seed exceeds archive limits")
    raw = path.read_bytes()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256) or _sha(raw) != expected_sha256:
        raise SeedError("archive SHA-256 mismatch")
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as stream:
        for item in stream:
            name = _safe_name(item.name)
            total += item.size
            if (not item.isfile() or item.mode != 0o600 or name in files
                    or len(files) >= MAX_FILES or total > MAX_ARCHIVE_BYTES):
                raise SeedError("unsafe or oversized archive member")
            files[name] = stream.extractfile(item).read()
    manifest = json.loads(files.pop("manifest.json"))
    if manifest["version"] != VERSION or set(manifest["files"]) != set(files):
        raise SeedError("invalid seed manifest")
    fixed = {"career.json", "registration.json", "model-support.json"} | {"standard-resume/" + name for name in STANDARD_FILES}
    if not fixed.issubset(files):
        raise SeedError("missing seed data")
    for name, data in files.items():
        if manifest["files"][name] != {"sha256": _sha(data), "size": len(data)}:
            raise SeedError("seed member checksum mismatch")
        if name not in fixed and not re.fullmatch(r"\.models/preference/runs/run_[A-Za-z0-9_]+/[A-Za-z0-9_.-]+", name):
            raise SeedError("unexpected seed member")
    return files


def _insert_rows(con, table: str, rows: list[dict[str, Any]]) -> None:
    allowed = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for row in rows:
        if set(row) != allowed:
            raise SeedError("model support schema mismatch")
        columns = sorted(allowed)
        con.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [row[key] for key in columns])


def import_seed(*, archive: Path, destination: Path, expected_sha256: str,
                embedding_identity: str, allow_empty_destination: bool = False,
                runtime_root: Path | None = None) -> dict[str, Any]:
    """Import into an absent private directory; never overwrite a live database."""
    destination = Path(destination).expanduser().absolute()
    existing_empty = False
    if destination.exists() or destination.is_symlink():
        # Repeated imports are explicitly reported and never reinitialize state.
        info = destination.lstat()
        if (not allow_empty_destination or not stat.S_ISDIR(info.st_mode)
                or destination != destination.resolve() or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or any(destination.iterdir())):
            raise SeedError("destination already exists; use status, never import over live state")
        existing_empty = True
    if not destination.parent.is_dir() or destination.parent != destination.parent.resolve():
        raise SeedError("destination parent must be an existing non-symlink directory")
    if not embedding_identity.strip():
        raise SeedError("target embedding identity is required")
    # Host paths and staging mounts usually differ from the final container mount.
    # The runtime root is a lexical target path, not a directory to create locally.
    runtime_root = Path(runtime_root) if runtime_root is not None else destination
    if not runtime_root.is_absolute() or runtime_root == Path("/") or ".." in runtime_root.parts:
        raise SeedError("runtime root must be an absolute non-root path without traversal")
    files = _read_archive(archive, expected_sha256)
    support = json.loads(files["model-support.json"])
    if set(support) != {"registry", "teacher"} or set(support["teacher"]) != set(PROXY_TABLES):
        raise SeedError("invalid model support tables")
    temporary = Path(tempfile.mkdtemp(prefix=".seed-import-", dir=destination.parent))
    try:
        for name, raw in files.items():
            if name.startswith(("standard-resume/", ".models/")):
                _private_write(temporary / name, raw)
        career = json.loads(files["career.json"])
        revision = career["current"]
        saved = CareerStore(temporary / "resume-lab.db").save_draft(
            revision["content"], actor="import",
            provenance={"kind": "aws_fresh_seed", "archive_sha256": expected_sha256,
                        "source_revision_id": revision["revision_id"],
                        "source_approved_revision_id": career["source_approved_revision_id"],
                        "source_provenance": revision["provenance"],
                        "attestation": "pending_user_review"},
            idempotency_key="aws_seed_" + expected_sha256[:24])
        registration = json.loads(files["registration.json"])
        service = ResumeLabService(temporary / "resume-lab.db")
        if registration:
            metadata = json.loads(registration["import_metadata_json"]) if registration["import_metadata_json"] else None
            if metadata:
                pdf = files["standard-resume/Sean_Katauskas_Resume.pdf"]
                if metadata.get("pdf_sha256") != _sha(pdf):
                    raise SeedError("registered standard PDF checksum mismatch")
                artifact = ResumePdfArtifactRepository(temporary / "resume-artifacts").write_pdf(pdf, ArtifactNamespace.REAL)
                metadata["managed_relative_path"] = artifact.managed_relative_path
            service.create_standard(registration["name"], registration["manual_rank"],
                StandardVersionInput(tex_source=registration["tex_source"], plain_text=registration["plain_text"],
                    claims=claims_from_json(registration["claims_json"]),
                    normalized_content=json.loads(registration["normalized_content_json"]) if registration["normalized_content_json"] else None,
                    import_metadata=metadata), actor_kind="user", active=bool(registration["active"]))
        else:
            (temporary / "resume-artifacts").mkdir(mode=0o700)
        from job_search.ranking import model as preference_model
        from job_search.ranking import proxy as preference_proxy
        preference_model.prepare_state(temporary / "job-boards-preference.db")
        preference_proxy.prepare_schema(temporary / "job-boards-proxy.db")
        model_results = []
        with sqlite3.connect(temporary / "job-boards-preference.db") as con:
            for row in support["registry"]:
                relative = ".models/preference/runs/" + row["run_id"]
                manifest = json.loads(files[relative + "/manifest.json"])
                if manifest != json.loads(row["manifest_json"]):
                    raise SeedError("model registry manifest mismatch")
                for name, digest in manifest["artifacts"].items():
                    if _sha(files[relative + "/" + _safe_name(name)]) != digest:
                        raise SeedError("model artifact checksum mismatch")
                row["artifact_path"] = str(runtime_root / relative)
                _insert_rows(con, "preference_model_runs", [row])
                model_results.append({"run_id": row["run_id"], "source_identity": row["model_revision"],
                    "status": "migration_required" if row["model_revision"] != embedding_identity else "imported_inactive"})
        with sqlite3.connect(temporary / "job-boards-proxy.db") as con:
            con.execute("PRAGMA foreign_keys=ON")
            # Some local teacher databases record the hosting provider separately
            # from teacher_model. Preserve that provenance in the fresh database;
            # all other schema differences still fail the exact-column check.
            teacher_runs = support["teacher"]["proxy_runs"]
            if any("teacher_provider" in row for row in teacher_runs):
                if any(not isinstance(row.get("teacher_provider"), str)
                       or len(row["teacher_provider"]) > 255 for row in teacher_runs):
                    raise SeedError("invalid teacher provider provenance")
                con.execute("ALTER TABLE proxy_runs ADD COLUMN teacher_provider TEXT NOT NULL DEFAULT ''")
            for row in support["teacher"]["proxy_students"]:
                row["state_db"] = str(runtime_root / "job-boards-preference.db")
                row["artifact_dir"] = str(runtime_root / ".models/preference")
            for table in PROXY_TABLES:
                _insert_rows(con, table, support["teacher"][table])
            if con.execute("PRAGMA foreign_key_check").fetchone():
                raise SeedError("teacher provenance foreign-key violation")
        for db in temporary.glob("*.db"):
            with sqlite3.connect(db) as con:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise SeedError("seed database integrity check failed")
        receipt = {"version": VERSION, "status": "imported", "archive_sha256": expected_sha256,
            "career_revision_id": saved["revision_id"], "career_approval": "pending_user_review",
            "standard_registered": bool(registration), "models": model_results,
            "embedding_identity": embedding_identity, "runtime_root": str(runtime_root), "champion_active": False}
        _private_write(temporary / "seed-receipt.json", _json(receipt))
        # SQLite creates auxiliary files under the ambient umask. Make the entire
        # staged tree private before publication; it has never been world-readable.
        for path in temporary.rglob("*"):
            try:
                path.chmod(0o700 if path.is_dir() else 0o600)
            except FileNotFoundError:
                # Closing a checkpointed SQLite connection can remove its WAL
                # or SHM between directory enumeration and chmod. Only those
                # ephemeral sidecars may disappear while the database remains.
                if not (path.name.endswith((".db-wal", ".db-shm"))
                        and path.with_name(path.name[:-4]).is_file()):
                    raise
        # Reserve destination atomically: competing imports cannot both succeed.
        if not existing_empty:
            destination.mkdir(mode=0o700)
        try:
            os.replace(temporary, destination)
        except BaseException:
            if not existing_empty:
                destination.rmdir()
            raise
        return receipt
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def seed_status(destination: Path) -> dict[str, Any]:
    """Return the import receipt without opening or migrating any database."""
    return json.loads(_source(Path(destination) / "seed-receipt.json", private=True).read_bytes())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="create a private selective seed, without copying operational databases")
    for name in ("career-db", "standard-dir", "preference-db", "proxy-db", "model-root", "output"):
        export.add_argument("--" + name, type=Path, required=True)
    export.add_argument("--run-id", action="append", dest="run_ids", required=True)
    export.add_argument("--offline-sources", action="store_true", help="assert source services are stopped and DBs checkpointed; refuse any WAL/journal")
    imp = sub.add_parser("import", help="import to a NEW state directory; never approve career facts or promote models")
    imp.add_argument("--archive", type=Path, required=True)
    imp.add_argument("--destination", type=Path, required=True)
    imp.add_argument("--sha256", dest="expected_sha256", required=True, help="archive hash reported by export, transferred separately")
    imp.add_argument("--embedding-identity", required=True, help="actual target provider's complete identity; changed identities require migration")
    imp.add_argument("--allow-empty-destination", action="store_true", help="also permit an existing owner-only EMPTY directory; refuse all live state")
    imp.add_argument("--runtime-root", type=Path, help="final container state root for stored model paths, e.g. /var/lib/job-search; defaults to destination")
    status = sub.add_parser("status", help="read the immutable import receipt; not runtime health")
    status.add_argument("--destination", type=Path, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        result = {"export": export_seed, "import": import_seed, "status": seed_status}[command](**args)
    except (SeedError, OSError, sqlite3.Error, ValueError, KeyError, tarfile.TarError):
        # Never include profile text, provider payloads, or secret paths in logs.
        parser.exit(1, "seed operation failed validation; inspect input paths, permissions, hashes, and schema\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
