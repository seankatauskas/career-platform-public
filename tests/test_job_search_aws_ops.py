"""Offline failure-injection tests for host backup, restore, and activation.

Use real SQLite WALs and archive bytes; mock only cloud/container boundaries and
host permissions. No AWS credentials, Docker daemon, or root privileges required.
"""
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from job_search import aws_ops as ops


class OperationsTests(unittest.TestCase):
    def setUp(self):
        for name in ("drain_workers", "wait_healthy", "stop_project", "project_containers"):
            mock = patch.object(ops, name)
            mocked = mock.start(); self.addCleanup(mock.stop)
            if name == "project_containers": mocked.return_value = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data"
        self.releases = self.root / "application"
        self.data.mkdir()
        self.releases.mkdir()
        self.c = {
            "version": 1, "aws_region": "us-east-2", "backup_bucket": "test-backups",
            "release_bucket": "test-releases", "data_root": str(self.data),
            "release_root": str(self.releases), "data_volume_id": "vol-0123456789abcdef0",
            "secret_arns": {}, "app_uid": os.getuid(), "app_gid": os.getgid(),
        }
        for name in (*ops.BACKUP_DIRS, "private", "runtime"):
            (self.data / name).mkdir()
        release = self.releases / "releases" / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1"
        release.mkdir(parents=True)
        pinned = "123456789012.dkr.ecr.us-east-2.amazonaws.com/career/app@sha256:" + "a" * 64
        (release / "release.json").write_text(json.dumps({
            "version": 1, "release_id": release.name, "source_sha": "a" * 40, "app_image": pinned,
            "operations_protocol": 1,
            "release_policy": {"version": 1, "schema_compatibility": "v1", "test_baseline_sha": "0" * 40, "predecessor": None},
            "transition_validation": {"schema_version": 1, "passed": True, "runtime": "docker", "source_sha": "a" * 40, "baseline_sha": "0" * 40, "rollback_passed": False},
            "hermes_image": pinned, "hermes_base_image": pinned,
            "tectonic_version": "tectonic 0.15.0", "schema_compatibility": "v1",
        }))
        (self.releases / "current").symlink_to(release)

    def bundle(self, *, extra_member=None):
        stage = self.root / "incoming"
        stage.mkdir()
        for name in ops.BACKUP_DIRS:
            (stage / name).mkdir()
            (stage / name / "restored.txt").write_text("new " + name)
        files = {str(p.relative_to(stage)): {"sha256": ops.digest(p), "size": p.stat().st_size}
                 for p in stage.rglob("*") if p.is_file()}
        manifest = {"version": 1, "backup_id": "backup-1", "release_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1",
                    "secrets": {}, "files": files}
        (stage / "backup.json").write_text(json.dumps(manifest))
        bundle = self.root / "incoming.tar.gz"
        with tarfile.open(bundle, "w:gz") as tar:
            for p in sorted(stage.rglob("*")):
                tar.add(p, arcname=str(p.relative_to(stage)), recursive=False)
            if extra_member is not None:
                tar.addfile(extra_member)
        return bundle

    def original_data(self):
        for name in ops.BACKUP_DIRS:
            (self.data / name / "original.txt").write_text("original " + name)

    def assert_original_data(self):
        for name in ops.BACKUP_DIRS:
            self.assertEqual((self.data / name / "original.txt").read_text(), "original " + name)
            self.assertFalse((self.data / name / "restored.txt").exists())

    def test_snapshot_includes_committed_wal_records(self):
        source = self.data / "state" / "applications.db"
        con = sqlite3.connect(source)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, stage TEXT)")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("INSERT INTO applications(stage) VALUES ('interview')")
        con.commit()
        self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)
        snapshot = self.root / "snapshot"
        ops.copy_snapshot(source.parent, snapshot)
        with sqlite3.connect((snapshot / source.name).as_uri() + "?immutable=1", uri=True) as restored:
            self.assertEqual(restored.execute("SELECT stage FROM applications").fetchall(), [("interview",)])
            self.assertEqual(restored.execute("PRAGMA quick_check").fetchone(), ("ok",))
        self.assertFalse(list(snapshot.glob("*-wal")))
        self.assertFalse(list(snapshot.glob("*-shm")))

    def test_first_deployment_snapshots_seed_without_current_release(self):
        (self.releases / "current").unlink()
        self.original_data()
        (self.data / "materialized-secrets.json").write_text("{}")
        with patch.object(ops, "run", return_value=""), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}):
            result = ops.deploy(self.c, "a" * 40 + "-1")
        self.assertEqual(result["status"], "deployed_paused")
        self.assertEqual(ops.release_path(self.c).name, "a" * 40 + "-1")
        self.assert_original_data()
        snapshot = ops.local_backup_path(self.c, result["backup"])
        self.assertEqual((snapshot / "state/original.txt").read_bytes(), b"original state")
        self.assertEqual(result["backup"]["format"], "directory-v1")
        self.assertEqual(ops.digest(snapshot / "backup.json"), result["backup"]["sha256"])

    def test_first_deployment_rejects_stray_one_shot_writer_before_snapshot(self):
        (self.releases / "current").unlink()
        self.original_data()
        with patch.object(ops, "compose", return_value=""), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "project_containers", return_value=["abc123def456"]), patch.object(ops, "backup_unlocked") as snapshot:
            with self.assertRaisesRegex(ops.OpsError, "writers failed to stop"):
                ops.deploy(self.c, "a" * 40 + "-1")
        snapshot.assert_not_called()
        self.assertFalse((self.releases / "current").exists())
        self.assert_original_data()

    def test_runtime_socket_directories_are_private_before_container_creation(self):
        tools = self.data / "runtime/tools"
        tools.mkdir(mode=0o755)
        with patch.object(ops.os, "chown") as chown:
            ops.chown_runtime(self.c)
        for name in ("tools", "notifications"):
            path = self.data / "runtime" / name
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            chown.assert_any_call(path, self.c["app_uid"], self.c["app_gid"])

    def test_failed_first_health_check_restores_seed_and_allows_real_retry(self):
        release_id = "a" * 40 + "-1"
        (self.releases / "current").unlink()
        self.original_data()
        (self.data / "materialized-secrets.json").write_text("{}")
        initialized = []
        def run(argv, **kwargs):
            if "initialize" in argv:
                initialized.append(True)
                (self.data / "state/candidate.txt").write_text("initialization")
            return ""
        with patch.object(ops, "run", side_effect=run), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}):
            with patch.object(ops, "wait_healthy", side_effect=ops.OpsError("tools unhealthy")):
                with self.assertRaisesRegex(ops.OpsError, "tools unhealthy"):
                    ops.deploy(self.c, release_id)
            self.assertFalse((self.releases / "current").exists())
            self.assert_original_data()
            self.assertFalse((self.data / "state/candidate.txt").exists())
            result = ops.deploy(self.c, release_id)
        self.assertEqual(result["status"], "deployed_paused")
        self.assertNotIn("already_installed", result)
        self.assertEqual(len(initialized), 2)

    def test_interrupted_first_install_recovers_seed_without_installed_pointer(self):
        release_id = "a" * 40 + "-1"
        (self.releases / "current").unlink()
        self.original_data()
        (self.data / "materialized-secrets.json").write_text("{}")
        snapshot = ops.backup_unlocked(self.c, paused=True, upload=False, release_id=release_id)
        op = ops.Operation.begin(self.c, "deploy", previous_release=None, target_release=release_id)
        op.update("initializing", state_mutation_started=True, backup=snapshot)
        ops.point_current(self.c, ops.release_path(self.c, release_id))
        (self.data / "state/original.txt").write_text("partially initialized")
        with patch.object(ops, "running_services", return_value=[]), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"):
            result = ops.recover(self.c, op.id)
        self.assertEqual(result["status"], "recovered_paused")
        self.assertFalse((self.releases / "current").exists())
        self.assert_original_data()

    def test_snapshot_rejects_symlink_to_unrelated_private_file(self):
        private = self.root / "do-not-copy"
        private.write_text("secret")
        (self.data / "state" / "link").symlink_to(private)
        with self.assertRaises(ops.OpsError):
            ops.copy_snapshot(self.data / "state", self.root / "snapshot")

    def local_snapshot(self):
        self.original_data()
        (self.data / "state/empty").mkdir(exist_ok=True)
        (self.data / "materialized-secrets.json").write_text("{}")
        receipt = ops.backup_unlocked(self.c, paused=True, upload=False)
        return receipt, ops.local_backup_path(self.c, receipt)

    def test_local_snapshot_skips_archiving_and_retains_all_durable_directories(self):
        with patch.object(ops.tarfile, "open", side_effect=AssertionError("no paused archive work")):
            receipt, snapshot = self.local_snapshot()
        manifest = ops.verify_snapshot(snapshot)
        self.assertEqual(set(manifest["files"]), {name + "/original.txt" for name in ops.BACKUP_DIRS})
        self.assertIn("state/empty", manifest["directories"])
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(snapshot.parent.glob("*.tar.gz")))
        self.assertFalse(list(snapshot.parent.glob("snapshot-pending-*")))
        self.assertEqual(receipt["file_count"], 3)
        self.assertGreaterEqual(receipt["timings_seconds"]["total"], 0)

    def test_directory_restore_preserves_backup_for_independent_retries(self):
        receipt, snapshot = self.local_snapshot()
        (self.data / "state/original.txt").write_text("candidate migration")
        with patch.object(ops, "running_services", return_value=[]), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"):
            ops.restore_unlocked(self.c, snapshot, receipt["sha256"], replace=True)
        self.assert_original_data()
        self.assertTrue((self.data / "state/empty").is_dir())
        self.assertEqual((snapshot / "state/original.txt").read_text(), "original state")
        self.assertNotEqual((snapshot / "state/original.txt").stat().st_ino,
                            (self.data / "state/original.txt").stat().st_ino)
        (self.data / "state/original.txt").write_text("new user work")
        ops.verify_snapshot(snapshot)

    def test_local_snapshot_capacity_reserves_usable_restore_headroom(self):
        self.original_data()
        (self.data / "materialized-secrets.json").write_text("{}")
        size = sum((self.data / name / "original.txt").stat().st_size for name in ops.BACKUP_DIRS)
        margin = 256 * 1024**2
        with patch.object(ops.shutil, "disk_usage", return_value=SimpleNamespace(free=size * 2 + margin)):
            receipt = ops.backup_unlocked(self.c, paused=True, upload=False)
        snapshot = ops.local_backup_path(self.c, receipt)
        # Capturing the snapshot used one payload's worth of initially reserved
        # space. Recovery must not require another TWO copies at this boundary.
        with patch.object(ops.shutil, "disk_usage", return_value=SimpleNamespace(free=size + margin)), patch.object(ops, "running_services", return_value=[]), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"):
            ops.restore_unlocked(self.c, snapshot, receipt["sha256"], replace=True)
        self.assert_original_data()

    def test_directory_restore_rejects_corruption_before_secret_or_data_mutation(self):
        cases = ("changed", "missing", "extra", "missing_directory", "extra_directory", "symlink", "fifo", "hardlink", "manifest")
        for case in cases:
            with self.subTest(case=case):
                fixture = OperationsTests(); fixture.setUp()
                try:
                    receipt, snapshot = fixture.local_snapshot()
                    item = snapshot / "state/original.txt"
                    if case == "changed": item.write_text("corrupt bytes")
                    elif case == "missing": item.unlink()
                    elif case == "extra": (snapshot / "state/unlisted.txt").write_text("extra")
                    elif case == "missing_directory": (snapshot / "state/empty").rmdir()
                    elif case == "extra_directory": (snapshot / "state/unlisted").mkdir()
                    elif case == "symlink": item.unlink(); item.symlink_to(fixture.data / "state/original.txt")
                    elif case == "fifo": item.unlink(); os.mkfifo(item)
                    elif case == "hardlink": item.unlink(); os.link(fixture.data / "state/original.txt", item)
                    elif case == "manifest": (snapshot / "backup.json").write_text("{}")
                    with patch.object(ops, "running_services", return_value=[]), patch.object(ops, "materialize_secrets") as secrets:
                        with self.assertRaises(ops.OpsError):
                            ops.restore_unlocked(fixture.c, snapshot, receipt["sha256"], replace=True)
                    secrets.assert_not_called()
                    fixture.assert_original_data()
                    self.assertIsNone(ops.read_operation(fixture.c))
                finally: fixture.doCleanups()

    def test_local_snapshot_sync_failure_cannot_publish_a_valid_receipt(self):
        self.original_data()
        (self.data / "materialized-secrets.json").write_text("{}")
        with patch.object(ops, "sync_tree", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError): ops.backup_unlocked(self.c, paused=True, upload=False)
        self.assertEqual(list((self.data / "backups").iterdir()), [])
        self.assert_original_data()

    def test_local_snapshot_rejects_symlinked_backup_root_and_unknown_format(self):
        external = self.root / "external"; external.mkdir()
        (self.data / "backups").symlink_to(external)
        with self.assertRaisesRegex(ops.OpsError, "symlink"):
            ops.backup_unlocked(self.c, paused=True, upload=False)
        with self.assertRaisesRegex(ops.OpsError, "format"):
            ops.local_backup_path(self.c, {"format": "future-format", "backup_id": "old"})
        self.assertEqual(list(external.iterdir()), [])

    def test_local_retention_counts_both_formats_and_preserves_unpublished_or_unknown_paths(self):
        backups = ops.backup_root(self.c)
        old_dir = backups / "20260101T000000-aaaaaaaa.snapshot"; old_dir.mkdir()
        (old_dir / "contents").write_text("expired fixture")
        middle = backups / "20260102T000000-bbbbbbbb.tar.gz"; middle.write_text("legacy archive")
        newest = backups / "20260103T000000-cccccccc.snapshot"; newest.mkdir()
        pending = backups / "snapshot-pending-fixture"; pending.mkdir()
        unknown = backups / "operator-evidence"; unknown.write_text("retain")
        link = backups / "20260104T000000-dddddddd.snapshot"; link.symlink_to(pending)
        ops.prune_local_backups(self.c, keep_backup="20260103T000000-cccccccc")
        self.assertFalse(old_dir.exists())
        self.assertTrue(all(path.exists() for path in (middle, newest, pending, unknown, link)))

    def test_cost_bind_source_is_public_observations_directory_not_private_credentials(self):
        costs = self.data / "costs"
        with patch.object(ops, "run", return_value="") as run:
            ops.compose(self.c, "config")
            self.assertFalse(costs.exists(), "diagnostics must not provision bind directories")
            ops.compose(self.c, "ps")
            self.assertFalse(costs.exists())
            ops.compose(self.c, "up", "-d", "dashboard")
        self.assertEqual(costs.stat().st_mode & 0o777, 0o755)
        self.assertEqual(run.call_args.kwargs["env"]["JOB_SEARCH_COST_DIR"], str(costs))
        costs.chmod(0o750)
        with patch.object(ops, "run", return_value=""), patch.object(ops.os, "chown") as chown:
            ops.compose(self.c, "ps")
        self.assertEqual(costs.stat().st_mode & 0o777, 0o750)
        chown.assert_not_called()
        costs.rmdir(); costs.symlink_to(self.data / "private")
        with patch.object(ops, "run") as run:
            with self.assertRaisesRegex(ops.OpsError, "symlink"): ops.compose(self.c, "config")
        run.assert_not_called()

    def test_snapshot_excludes_hermes_credentials_and_runtime_logs(self):
        hermes = self.data / "hermes"
        (hermes / ".env").write_text("TOKEN=secret")
        (hermes / "config.yaml").write_text("secret: yes")
        (hermes / "history.db").write_text("retained history")
        (hermes / "logs").mkdir()
        (hermes / "logs" / "sensitive.txt").write_text("prompt content")
        snapshot = self.root / "snapshot"
        ops.copy_snapshot(hermes, snapshot)
        self.assertEqual([p.name for p in snapshot.iterdir()], ["history.db"])

    def test_backup_after_hermes_startup_retains_database_and_prunes_only_ephemeral_files(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            hermes = Path(temp).resolve()
            cache = hermes / "home/.cache/uv/wheels"
            cache.mkdir(parents=True)
            (cache / "package").symlink_to("/unrelated/private")
            (hermes / "state").mkdir()
            for name in ("gateway.sock", "state/gateway.loop-tick.177.sock"):
                sock = socket.socket(socket.AF_UNIX)
                try: sock.bind(str(hermes / name))
                finally: sock.close()
            with sqlite3.connect(hermes / "state/history.db") as db:
                db.execute("CREATE TABLE history (message TEXT)")
                db.execute("INSERT INTO history VALUES ('retained')")
            snapshot = self.root / "snapshot"
            ops.copy_snapshot(hermes, snapshot, hermes=True)
            with sqlite3.connect(snapshot / "state/history.db") as db:
                self.assertEqual(db.execute("SELECT message FROM history").fetchone(), ("retained",))
            self.assertFalse((snapshot / "home/.cache/uv").exists())
            self.assertFalse(list(snapshot.rglob("*.sock")))
            # The same paths in ordinary application state are not exempt.
            with self.assertRaises(ops.OpsError):
                ops.copy_snapshot(hermes, self.root / "ordinary")
            (hermes / "gateway.sock").unlink()
            (hermes / "gateway.sock").symlink_to("/unrelated/private")
            with self.assertRaises(ops.OpsError):
                ops.copy_snapshot(hermes, self.root / "unsafe", hermes=True)

    def test_runtime_ownership_skips_uv_links_but_rejects_other_links(self):
        cache = self.data / "hermes/home/.cache/uv"
        cache.mkdir(parents=True)
        link = cache / "package"
        link.symlink_to("/unrelated/private")
        with patch.object(ops.os, "chown") as chown:
            ops.chown_runtime(self.c)
            self.assertNotIn(link, [call.args[0] for call in chown.call_args_list])
            (cache.parent / "unexpected").symlink_to("/unrelated/private")
            with self.assertRaises(ops.OpsError): ops.chown_runtime(self.c)

    def test_hermes_cache_root_and_unknown_sockets_are_not_trusted(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            hermes = Path(temp).resolve()
            (hermes / "home/.cache").mkdir(parents=True)
            cache = hermes / "home/.cache/uv"
            cache.symlink_to("/unrelated/private")
            with self.assertRaises(ops.OpsError):
                ops.copy_snapshot(hermes, self.root / "linked-cache", hermes=True)
            cache.unlink()
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.bind(str(hermes / "unknown.sock"))
                with self.assertRaises(ops.OpsError):
                    ops.copy_snapshot(hermes, self.root / "unknown-socket", hermes=True)
            finally: sock.close()

    def test_archive_rejects_traversal_symlinks_and_duplicate_paths(self):
        for case in ("traversal", "symlink", "duplicate"):
            with self.subTest(case=case):
                bundle = self.root / (case + ".tar.gz")
                with tarfile.open(bundle, "w:gz") as tar:
                    item = tarfile.TarInfo("../escaped" if case == "traversal" else "state/example")
                    if case == "symlink":
                        item.type = tarfile.SYMTYPE
                        item.linkname = "/etc/shadow"
                    tar.addfile(item)
                    if case == "duplicate": tar.addfile(item)
                target = self.root / case
                target.mkdir()
                with self.assertRaises(ops.OpsError): ops.safe_extract(bundle, target)
        self.assertFalse((self.root / "escaped").exists())

    def test_archive_expansion_limit(self):
        bundle = self.root / "oversized.tar.gz"
        with tarfile.open(bundle, "w:gz") as tar:
            item = tarfile.TarInfo("state/payload")
            item.size = 1024
            tar.addfile(item, io.BytesIO(b"x" * item.size))
        target = self.root / "extract"
        target.mkdir()
        with self.assertRaises(ops.OpsError): ops.safe_extract(bundle, target, max_bytes=512)

    def test_manifest_detects_changed_bytes_and_unlisted_files(self):
        self.bundle()
        stage = self.root / "incoming"
        original = (stage / "state" / "restored.txt").read_bytes()
        (stage / "state" / "restored.txt").write_bytes(b"x" * len(original))
        with self.assertRaisesRegex(ops.OpsError, "checksum"): ops.verify_snapshot(stage)
        (stage / "state" / "restored.txt").write_bytes(original)
        (stage / "state" / "unlisted").write_text("unexpected")
        with self.assertRaisesRegex(ops.OpsError, "file set"): ops.verify_snapshot(stage)

    def test_archive_checksum_checked_before_touching_existing_state(self):
        self.original_data()
        bundle = self.bundle()
        with patch.object(ops, "materialize_secrets") as secrets:
            with self.assertRaisesRegex(ops.OpsError, "archive checksum"):
                ops.restore_unlocked(self.c, bundle, "0" * 64, replace=True)
        secrets.assert_not_called()
        self.assert_original_data()

    def test_failed_quiesce_restarts_previously_running_services(self):
        calls = []
        def compose(c, *args, **kwargs):
            calls.append(args)
            if args[0] == "stop": raise ops.OpsError("simulated Docker timeout")
            return ""
        with patch.object(ops, "running_services", return_value=["core", "model"]), patch.object(ops, "compose", side_effect=compose):
            with self.assertRaises(ops.OpsError):
                with ops.quiesced(self.c):
                    self.fail("capture must not begin after stop fails")
        self.assertEqual(calls[-1], ("up", "-d", "--no-deps", "--no-build", "core", "model"))

    def test_capture_failure_still_restarts_services(self):
        with patch.object(ops, "running_services", side_effect=[["core"], []]), patch.object(ops, "compose", return_value="") as compose:
            with self.assertRaisesRegex(ValueError, "disk full"):
                with ops.quiesced(self.c): raise ValueError("disk full")
        self.assertEqual(compose.call_args.args[1:], ("up", "-d", "--no-deps", "--no-build", "core"))

    def test_missing_encryption_key_aborts_restore_before_replacing_data(self):
        self.original_data()
        bundle = self.bundle()
        with patch.object(ops, "running_services", return_value=[]), patch.object(ops, "materialize_secrets", side_effect=ops.OpsError("required key unavailable")):
            with self.assertRaisesRegex(ops.OpsError, "key unavailable"):
                ops.restore_unlocked(self.c, bundle, ops.digest(bundle), replace=True)
        self.assert_original_data()
        self.assertFalse(list(self.data.glob("pre-restore-*")))

    def test_partial_directory_publication_rolls_back_all_data(self):
        self.original_data()
        bundle = self.bundle()
        real_replace = os.replace
        def fail_hermes_publish(source, destination):
            source, destination = Path(source), Path(destination)
            if source.name == "hermes" and source.parent.name.startswith("restore-") and destination == self.data / "hermes":
                raise OSError("injected failure publishing second directory")
            return real_replace(source, destination)
        with patch.object(ops, "running_services", return_value=[]), patch.object(ops, "materialize_secrets"), patch.object(ops.os, "replace", side_effect=fail_hermes_publish):
            with self.assertRaises(OSError): ops.restore_unlocked(self.c, bundle, ops.digest(bundle), replace=True)
        self.assert_original_data()
        self.assertEqual(ops.release_path(self.c).name, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1")

    def test_postpublication_secret_failure_rolls_back_original_data(self):
        self.original_data()
        bundle = self.bundle()
        with patch.object(ops, "running_services", return_value=[]), patch.object(ops, "materialize_secrets", side_effect=[None, ops.OpsError("secret provider unavailable"), None]), patch.object(ops, "chown_runtime"):
            with self.assertRaisesRegex(ops.OpsError, "secret provider"):
                ops.restore_unlocked(self.c, bundle, ops.digest(bundle), replace=True)
        self.assert_original_data()

    def test_restore_remains_paused_after_success(self):
        self.original_data()
        bundle = self.bundle()
        with patch.object(ops, "running_services", return_value=[]), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "compose") as compose:
            result = ops.restore_unlocked(self.c, bundle, ops.digest(bundle), replace=True)
        self.assertEqual(result["status"], "restored_paused")
        self.assertFalse(json.loads((self.data / "activation.json").read_text())["enabled"])
        self.assertEqual((self.data / "state" / "restored.txt").read_text(), "new state")
        compose.assert_not_called()

    def test_backup_upload_failure_restarts_writers_and_does_not_mark_success(self):
        self.original_data()
        with patch.object(ops, "running_services", side_effect=[["core", "model"], []]), patch.object(ops, "compose", return_value="") as compose, patch.object(ops, "secret_versions", return_value={}), patch.object(ops, "aws", side_effect=ops.OpsError("upload failed")):
            with self.assertRaisesRegex(ops.OpsError, "upload failed"):
                ops.backup_unlocked(self.c)
        self.assertEqual(compose.call_args.args[1:], ("up", "-d", "--no-deps", "--no-build", "core", "model"))
        self.assertFalse((self.data / "last-backup.json").exists())
        self.assert_original_data()

    def test_backup_records_used_secret_version_not_latest_remote_version(self):
        arn = "arn:aws:secretsmanager:us-east-2:123456789012:secret:career-platform/portable-master-key-abc123"
        self.c["secret_arns"] = {"portable-master-key": arn}
        with patch.object(ops, "aws", return_value=json.dumps({"SecretString": "original-key", "VersionId": "version-original"})), patch.object(ops.os, "chown"):
            ops.materialize_secrets(self.c)
        with patch.object(ops, "aws", side_effect=AssertionError("must not query mutable AWSCURRENT")):
            recorded = ops.secret_versions(self.c)
        self.assertEqual(recorded, {"portable-master-key": {"arn": arn, "version_id": "version-original"}})

    def test_changed_encryption_key_cannot_partially_update_other_secrets(self):
        self.c["secret_arns"] = {"config.json": "config-arn", "portable-master-key": "key-arn"}
        (self.data / "private" / "config.json").write_text("old-config")
        (self.data / "private" / "portable-master-key").write_text("old-key")
        values = [json.dumps({"SecretString": "new-config", "VersionId": "config-v2"}), json.dumps({"SecretString": "different-key", "VersionId": "key-v2"})]
        with patch.object(ops, "aws", side_effect=values):
            with self.assertRaisesRegex(ops.OpsError, "encryption key changed"):
                ops.materialize_secrets(self.c)
        self.assertEqual((self.data / "private" / "config.json").read_text(), "old-config")
        self.assertEqual((self.data / "private" / "portable-master-key").read_text(), "old-key")

    def test_model_readiness_rejects_unmigrated_models_and_missing_dashboard_owner(self):
        reports = [
            {"configured": False, "embedding": "ready", "private_dashboard": True},
            {"configured": True, "embedding": "migration_required", "private_dashboard": True},
            {"configured": True, "embedding": "identity_mismatch", "private_dashboard": True},
            {"configured": True, "embedding": "ready", "private_dashboard": False},
            {"configured": True, "embedding": "ready", "private_dashboard": True, "mail_ready": False},
        ]
        for report in reports:
            with self.subTest(report=report), patch.object(ops, "compose", return_value=json.dumps(report)):
                with self.assertRaises(ops.OpsError): ops.model_readiness(self.c)
        with patch.object(ops, "compose", return_value=json.dumps({"configured": True, "embedding": "ready", "private_dashboard": True})):
            ops.model_readiness(self.c)

    def test_sparse_cpu_activation_requires_validated_mode_and_policy_without_embeddings(self):
        ready = {"configured": True, "embedding": "not_required", "private_dashboard": True,
                 "ranking_refresh_mode": "sparse_cpu", "shortlist_policy": "compare", "mail_ready": True}
        with patch.object(ops, "compose", return_value=json.dumps(ready)) as compose:
            ops.model_readiness(self.c)
        probe = compose.call_args.args[-1]
        self.assertIn("load_runtime_config", probe)
        self.assertIn("c.ranking_refresh_mode", probe)
        self.assertNotIn("salary", probe)
        for change in ({"configured": False}, {"ranking_refresh_mode": "full"},
                       {"ranking_refresh_mode": "broad_cpu"}, {"shortlist_policy": "champion"},
                       {"embedding": "migration_required"}, {"mail_ready": False}):
            with self.subTest(change=change), patch.object(ops, "compose", return_value=json.dumps({**ready, **change})):
                with self.assertRaises(ops.OpsError):
                    ops.model_readiness(self.c)

    def mail_secret_values(self):
        profile = {"version": 1, "profile_id": "mail", "embeddings": None,
                   "structured_generation": {"kind": "openrouter", "model": "example/model",
                                             "credential_file": "/run/job-search/openrouter-api-key"}}
        return {"config.json": json.dumps({"mail_inference_config": "/run/job-search/mail-inference.json",
                                           "mail_inference_profile": profile}),
                "hermes.env": 'OPENROUTER_API_KEY="fictional-key"\nTELEGRAM_BOT_TOKEN=not-for-core\n'}

    def test_mail_projection_exposes_only_selected_key_and_profile(self):
        self.assertEqual(ops.mail_secret_projections({"config.json": '{"mail_inference_profile":null}'}), {})
        self.assertEqual(ops.mail_secret_projections({"config.json": '{}'}), {})
        values = self.mail_secret_values()
        projected = ops.mail_secret_projections(values)
        self.assertEqual(projected["openrouter-api-key"], "fictional-key\n")
        self.assertEqual(set(projected), {"mail-inference.json", "openrouter-api-key"})
        self.assertNotIn("not-for-core", json.dumps(projected))
        self.c["secret_arns"] = {name: name + "-arn" for name in values}
        responses = [json.dumps({"SecretString": value, "VersionId": name + "-version"}) for name, value in values.items()]
        with patch.object(ops, "aws", side_effect=responses), patch.object(ops.os, "chown"):
            ops.materialize_secrets(self.c)
        for name, value in projected.items():
            path = self.data / "private" / name
            self.assertEqual(path.read_text(), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(ops.secret_versions(self.c)), set(values))

    def test_mail_projection_rejects_missing_duplicate_and_malformed_keys(self):
        for env in ("", "OPENROUTER_API_KEY=one\nOPENROUTER_API_KEY=two", 'OPENROUTER_API_KEY="unfinished', "OPENROUTER_API_KEY=two words", "OPENROUTER_API_KEY=\nTELEGRAM_BOT_TOKEN=not-a-model-key"):
            with self.subTest(env=env):
                values = self.mail_secret_values(); values["hermes.env"] = env
                with self.assertRaisesRegex(ops.OpsError, "one valid OpenRouter"):
                    ops.mail_secret_projections(values)

    def test_secret_transaction_restores_projections(self):
        profile = self.data / "private" / "mail-inference.json"
        key = self.data / "private" / "openrouter-api-key"
        profile.write_text("old-profile")
        with self.assertRaises(RuntimeError):
            with ops.secret_transaction(self.c):
                profile.write_text("new-profile"); key.write_text("new-key")
                raise RuntimeError("interrupted")
        self.assertEqual(profile.read_text(), "old-profile")
        self.assertFalse(key.exists())

    def test_mail_overlay_is_opt_in_and_legacy_release_remains_usable(self):
        release = ops.release_path(self.c)
        (release / "compose.mail.yaml").write_text("services: {}")
        config = self.data / "private" / "config.json"
        config.write_text("{}")
        with patch.object(ops, "run", return_value="") as run:
            ops.compose(self.c, "config")
            self.assertNotIn(str(release / "compose.mail.yaml"), run.call_args.args[0])
            config.write_text(json.dumps({"mail_inference_config": "/run/job-search/mail-inference.json"}))
            ops.compose(self.c, "config")
            self.assertIn(str(release / "compose.mail.yaml"), run.call_args.args[0])
            (release / "compose.mail.yaml").unlink()
            ops.compose(self.c, "config")
            self.assertNotIn(str(release / "compose.mail.yaml"), run.call_args.args[0])

    def test_model_readiness_failure_prevents_activation(self):
        with patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "model_readiness", side_effect=ops.OpsError("migration incomplete")), patch.object(ops, "compose") as compose:
            with self.assertRaisesRegex(ops.OpsError, "migration incomplete"):
                ops.activate(self.c)
        compose.assert_not_called()
        self.assertFalse((self.data / "activation.json").exists())

    def install_candidate(self):
        target = self.releases / "releases" / "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-2"
        target.mkdir()
        candidate = json.loads((ops.release_path(self.c) / "release.json").read_text())
        candidate["release_id"] = target.name
        candidate["source_sha"] = "b" * 40
        candidate["release_policy"]["test_baseline_sha"] = "a" * 40
        candidate["release_policy"]["predecessor"] = {"release_id": "a" * 40 + "-1", "source_sha": "a" * 40}
        candidate["transition_validation"].update(source_sha="b" * 40, baseline_sha="a" * 40, rollback_passed=True)
        (target / "release.json").write_text(json.dumps(candidate))
        return target

    def test_failed_upgrade_before_writes_restores_release_and_activation(self):
        self.install_candidate()
        original_activation = {"enabled": True, "operator_note": "accepted pilot"}
        (self.data / "activation.json").write_text(json.dumps(original_activation))
        calls = []
        def compose(c, *args, **kwargs):
            calls.append(args)
            if args[0] == "run" and "initialize" in args:
                raise ops.OpsError("candidate initialization failed")
            return ""
        def restored(*args, **kwargs):
            (self.data / "activation.json").write_text('{"enabled": false}')
            return {"status": "restored_paused"}
        with patch.object(ops, "running_services", side_effect=[["core", "model"], []]), patch.object(ops, "compose", side_effect=compose), patch.object(ops, "backup_unlocked", return_value={"backup_id": "before-upgrade", "sha256": "a" * 64}), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "restore_unlocked", side_effect=restored) as restore:
            with self.assertRaisesRegex(ops.OpsError, "initialization failed"):
                ops.deploy(self.c, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-2")
        restore.assert_called_once()
        self.assertEqual(ops.release_path(self.c).name, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1")
        self.assertEqual(json.loads((self.data / "activation.json").read_text()), original_activation)
        self.assertEqual(calls[-1], ("up", "-d", "--no-deps", "--no-build", "--force-recreate", "core", "model"))

    def test_upgrade_captures_previous_state_before_refreshing_secrets(self):
        self.install_candidate()
        (self.data / "activation.json").write_text('{"enabled": true}')
        events = []
        def backup(*args, **kwargs):
            events.append("backup")
            self.assertEqual(ops.release_path(self.c).name, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1")
            self.assertTrue(kwargs["paused"])
            return {"backup_id": "before-upgrade", "sha256": "a" * 64}
        def compose(c, *args, **kwargs):
            if args[0] == "stop": events.append("stop")
            return ""
        with patch.object(ops, "running_services", side_effect=[["core"], []]), patch.object(ops, "compose", side_effect=compose), patch.object(ops, "backup_unlocked", side_effect=backup), patch.object(ops, "materialize_secrets", side_effect=lambda c: events.append("secrets")), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "wait_healthy"), patch.object(ops, "model_readiness"):
            result = ops.deploy(self.c, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-2")
        self.assertEqual(events[:3], ["stop", "backup", "secrets"])
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(ops.release_path(self.c).name, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-2")

    def test_retention_failure_does_not_misreport_completed_deployment_or_pause_writers(self):
        self.install_candidate()
        with patch.object(ops, "running_services", return_value=["dashboard"]), patch.object(ops, "compose", return_value=""), patch.object(ops, "backup_unlocked", return_value={"backup_id": "before-upgrade", "sha256": "a" * 64}), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "prune_local_backups", side_effect=OSError("cleanup failed")), patch.object(ops, "pause") as pause:
            result = ops.deploy(self.c, "b" * 40 + "-2")
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(result["local_backup_retention"], "cleanup_failed_backups_preserved")
        self.assertTrue(ops.read_operation(self.c)["complete"])
        pause.assert_not_called()

    def test_image_retention_runs_after_deployment_commits_and_health_passes(self):
        target = self.install_candidate()
        previous = ops.release_path(self.c)
        def retain(c, **kwargs):
            self.assertTrue(ops.read_operation(c)["complete"])
            self.assertEqual(ops.read_operation(c)["phase"], "deployed")
            self.assertEqual(kwargs, {"current": target, "previous": previous})
            return {"status": "retained", "removed_refs": 3}
        with patch.object(ops, "running_services", return_value=["dashboard"]), patch.object(ops, "compose", return_value=""), patch.object(ops, "backup_unlocked", return_value={"backup_id": "before-upgrade", "sha256": "a" * 64}), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "prune_local_images", side_effect=retain) as cleanup:
            result = ops.deploy(self.c, target.name)
        cleanup.assert_called_once()
        self.assertEqual(result["local_image_retention"]["removed_refs"], 3)

    def test_image_retention_failure_is_a_warning_on_completed_deployment(self):
        target = self.install_candidate()
        with patch.object(ops, "running_services", return_value=["dashboard"]), patch.object(ops, "compose", return_value=""), patch.object(ops, "backup_unlocked", return_value={"backup_id": "before-upgrade", "sha256": "a" * 64}), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "prune_local_images", side_effect=ValueError("bad Docker response")), patch.object(ops, "pause") as pause:
            result = ops.deploy(self.c, target.name)
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(result["local_image_retention"], {"status": "cleanup_failed"})
        pause.assert_not_called()

    def test_image_retention_never_runs_after_failed_deployment(self):
        target = self.install_candidate()
        with patch.object(ops, "compose", return_value=""), patch.object(ops, "preflight", return_value={"issues": ["disk"]}), patch.object(ops, "prune_local_images") as cleanup:
            with self.assertRaises(ops.OpsError):
                ops.deploy(self.c, target.name)
        cleanup.assert_not_called()

    def test_postresume_health_failure_stops_services_without_rewinding_data(self):
        self.install_candidate()
        (self.data / "activation.json").write_text('{"enabled": true}')
        def check(c, expected):
            if "core" in expected:
                # Represents an application write after the rollback-safe boundary.
                (self.data / "state" / "new-application.txt").write_text("submitted after upgrade")
                raise ops.OpsError("new worker unhealthy")
        with patch.object(ops, "running_services", side_effect=[["core", "model"], [], []]), patch.object(ops, "compose", return_value="") as compose, patch.object(ops, "backup_unlocked", return_value={"backup_id": "before-upgrade", "sha256": "a" * 64}), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "model_readiness"), patch.object(ops, "wait_healthy", side_effect=check), patch.object(ops, "restore_unlocked") as restore:
            with self.assertRaisesRegex(ops.OpsError, "new worker unhealthy"):
                ops.deploy(self.c, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-2")
        restore.assert_not_called()
        self.assertEqual(ops.release_path(self.c).name, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-2")
        self.assertEqual((self.data / "state" / "new-application.txt").read_text(), "submitted after upgrade")
        self.assertFalse(json.loads((self.data / "activation.json").read_text())["enabled"])
        self.assertEqual(compose.call_args.args[1:], ("stop", "--timeout", "4200", *ops.SERVICES))

    def test_review_starts_interactive_services_without_recurring_workers(self):
        with patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "running_services", return_value=[]), patch.object(ops, "compose", return_value="") as compose, patch.object(ops, "wait_healthy"), patch.object(ops, "model_readiness") as model:
            result = ops.review(self.c)
        self.assertFalse(result["recurring_workers_enabled"])
        self.assertEqual(json.loads((self.data / "activation.json").read_text()), {"enabled": False, "mode": "review"})
        ups = [call.args[1:] for call in compose.call_args_list if call.args[1] == "up"]
        self.assertEqual(len(ups), 1)
        self.assertEqual({value for value in ups[0][2:] if not value.startswith("-")}, {"tools", "dashboard", "mcp", "hermes"})
        self.assertNotIn("core", ups[0])
        self.assertNotIn("model", ups[0])
        model.assert_not_called()

    def test_upgrade_preserves_review_mode_without_enabling_workers(self):
        self.install_candidate()
        original_activation = {"enabled": False, "mode": "review"}
        (self.data / "activation.json").write_text(json.dumps(original_activation))
        with patch.object(ops, "running_services", side_effect=[list(ops.REVIEW_SERVICES), []]), patch.object(ops, "compose", return_value="") as compose, patch.object(ops, "backup_unlocked", return_value={"backup_id": "before-upgrade", "sha256": "a" * 64}), patch.object(ops, "materialize_secrets"), patch.object(ops, "chown_runtime"), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "wait_healthy"), patch.object(ops, "model_readiness") as model:
            ops.deploy(self.c, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-2")
        self.assertEqual(json.loads((self.data / "activation.json").read_text()), original_activation)
        for call in compose.call_args_list:
            if call.args[1] == "up":
                self.assertNotIn("core", call.args)
                self.assertNotIn("model", call.args)
        model.assert_not_called()

    def domain(self, status="ready", stale=0, failed=0, reconcile=0):
        return {"schema_version": 1, "status": status, "metrics": {
            "stale_capabilities": stale, "unresolved_work": failed,
            "pending_reconciliation": reconcile,
        }}

    def test_domain_probe_uses_running_core_without_starting_services(self):
        with patch.object(ops, "compose", return_value=json.dumps(self.domain())) as compose:
            result = ops.domain_readiness(self.c)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(compose.call_args.args[1:4], ("exec", "-T", "core"))
        self.assertEqual(compose.call_args.args[-1], "readiness")

    def test_domain_probe_rejects_untrusted_shapes(self):
        for report in ({}, {"schema_version": 1, "status": "healthy"}, self.domain(stale=-1), self.domain(reconcile=True)):
            with self.subTest(report=report), patch.object(ops, "compose", return_value=json.dumps(report)):
                with self.assertRaises(ops.OpsError):
                    ops.domain_readiness(self.c)

    def test_domain_failure_does_not_change_liveness_metric(self):
        (self.data / "activation.json").write_text('{"enabled": true}')
        self.c["instance_id"] = "i-fixture"
        with patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "healthy", return_value=True), patch.object(ops, "domain_readiness", return_value=self.domain("blocked", failed=2, reconcile=1)), patch.object(ops, "aws") as aws:
            result = ops.status(self.c, publish=True)
        self.assertEqual(result["status"], "attention")
        self.assertTrue(result["liveness_healthy"])
        metrics = {item["MetricName"]: item["Value"] for item in json.loads(aws.call_args.args[-1])}
        self.assertEqual(metrics["Healthy"], 1)
        self.assertEqual(metrics["DomainReady"], 0)
        self.assertEqual(metrics["DomainPendingReconciliation"], 1)

    def test_paused_stack_does_not_probe_or_start_a_worker(self):
        (self.data / "activation.json").write_text('{"enabled": false}')
        with patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "running_services", return_value=[]), patch.object(ops, "domain_readiness") as probe:
            result = ops.status(self.c)
        probe.assert_not_called()
        self.assertEqual(result["domain"]["status"], "paused")

    def test_host_memory_snapshot_is_local_and_does_not_expand_cloudwatch_metrics(self):
        (self.data / "activation.json").write_text('{"enabled": false}')
        self.c["instance_id"] = "i-fixture"
        memory = {"host_available_bytes": 1024, "host_file_cache_bytes": 2048,
                  "process_rss_bytes": 512, "cgroup_current_bytes": 4096}
        with patch.object(ops, "preflight", return_value={"issues": []}), \
             patch.object(ops, "running_services", return_value=[]), \
             patch("job_search.resource_usage.memory_snapshot", return_value=memory), \
             patch.object(ops, "aws") as aws:
            result = ops.status(self.c, publish=True)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["host_memory"]["counters"],
                         {"host_available_bytes": 1024, "host_file_cache_bytes": 2048})
        metrics = json.loads(aws.call_args.args[-1])
        self.assertEqual(len(metrics), 8)
        self.assertFalse(any("memory" in item["MetricName"].lower() for item in metrics))
        with patch.object(ops, "preflight", return_value={"issues": []}), \
             patch.object(ops, "running_services", return_value=[]), \
             patch("job_search.resource_usage.memory_snapshot", side_effect=OSError("private failure")):
            result = ops.status(self.c)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["host_memory"], {"status": "unavailable"})

    def test_unavailable_domain_report_is_visible(self):
        (self.data / "activation.json").write_text('{"enabled": true}')
        with patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "healthy", return_value=True), patch.object(ops, "domain_readiness", side_effect=ops.OpsError("fixture")):
            result = ops.status(self.c)
        self.assertEqual(result["domain"]["reason_code"], "domain_report_unavailable")
        self.assertEqual(result["status"], "attention")

    def test_startup_grace_is_not_a_domain_alarm(self):
        (self.data / "activation.json").write_text('{"enabled": true}')
        with patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "healthy", return_value=True), patch.object(ops, "domain_readiness", return_value=self.domain("configured_unverified")):
            result = ops.status(self.c)
        self.assertEqual(result["status"], "healthy")

    def test_partial_pause_is_not_a_domain_alarm(self):
        (self.data / "activation.json").write_text('{"enabled": true}')
        with patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "healthy", return_value=True), patch.object(ops, "domain_readiness", return_value=self.domain("paused")):
            self.assertEqual(ops.status(self.c)["status"], "healthy")

    def test_monitor_success_is_distinct_from_unhealthy_report_or_publication_failure(self):
        with patch.object(ops, "load_config", return_value=self.c), patch.object(ops, "status", return_value={"status": "attention"}), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(ops.main(["status"]), 2)
            self.assertEqual(ops.main(["status", "--publish"]), 0)
        with patch.object(ops, "load_config", return_value=self.c), patch.object(ops, "status", side_effect=ops.OpsError("publication failed")), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(ops.main(["status", "--publish"]), 2)

    def test_only_scheduled_backup_treats_positive_lock_contention_as_deferred(self):
        with patch.object(ops, "load_config", return_value=self.c), patch.object(ops.os, "geteuid", return_value=0), patch.object(ops, "lock", side_effect=ops.OperationBusy("busy")), patch.object(ops, "scheduled_backup") as backup, patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(ops.main(["backup", "--scheduled"]), 0)
            self.assertEqual(json.loads(out.getvalue())["status"], "deferred")
            self.assertEqual(ops.main(["backup"]), 2)
            backup.assert_not_called()
        with patch.object(ops, "load_config", return_value=self.c), patch.object(ops.os, "geteuid", return_value=0), patch.object(ops, "lock", side_effect=ops.OpsError("unsafe lock")), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(ops.main(["backup", "--scheduled"]), 2)

    def test_cli_incomplete_preflight_never_starts_services(self):
        with patch.object(ops, "load_config", return_value=self.c), patch.object(ops.os, "geteuid", return_value=0), patch.object(ops, "lock", return_value=nullcontext()), patch.object(ops, "preflight", return_value={"issues": ["missing_model"]}), patch.object(ops, "compose") as compose, patch("sys.stderr", new_callable=io.StringIO):
            result = ops.main(["activate"])
        self.assertEqual(result, 2)
        compose.assert_not_called()

    def test_failed_activation_stops_services_and_clears_enabled_flag(self):
        (self.data / "activation.json").write_text('{"enabled": true}')
        # A prior enabled flag must not survive a failed reactivation.
        with patch.object(ops, "load_config", return_value=self.c), patch.object(ops.os, "geteuid", return_value=0), patch.object(ops, "lock", return_value=nullcontext()), patch.object(ops, "preflight", return_value={"issues": []}), patch.object(ops, "model_readiness", return_value=None), patch.object(ops, "compose", return_value="") as compose, patch.object(ops, "wait_healthy", side_effect=ops.OpsError("unhealthy candidate")), patch("sys.stderr", new_callable=io.StringIO):
            result = ops.main(["activate"])
        self.assertEqual(result, 2)
        self.assertFalse(json.loads((self.data / "activation.json").read_text())["enabled"])
        self.assertTrue(any(call.args[1] == "stop" for call in compose.call_args_list))


if __name__ == "__main__":
    unittest.main()
