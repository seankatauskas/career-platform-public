"""Offline fresh-seed boundaries, round trips, and corruption checks."""

import hashlib
import io
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from job_search.ranking import model as preference_model
from job_search.ranking import proxy as preference_proxy
from job_search.aws_seed import (
    STANDARD_FILES, SeedError, export_seed, import_seed, seed_status,
)
from job_search.resume_lab.career_store import CareerStore, empty_career_content
from job_search.resume_lab.contracts import StandardVersionInput, ResumeClaim, ClaimOrigin
from job_search.resume_lab.service import ResumeLabService


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.career = self.root / "career.db"
        content = empty_career_content()
        content["identity"]["name"] = "Example Person"
        content["projects"] = [{"name": "Planner", "context": "Python", "dates": "2026", "url": "", "bullets": ["Developed a planner."]}]
        self.store = CareerStore(self.career)
        self.revision = self.store.save_draft(content)
        ResumeLabService(self.career)
        self.standard = self.root / "standard"
        self.standard.mkdir()
        for filename in STANDARD_FILES:
            data = b"{}" if filename.endswith(".json") else b"Example resume"
            (self.standard / filename).write_bytes(data)
        self.preference = self.root / "preference.db"
        self.proxy = self.root / "proxy.db"
        preference_model.prepare_state(self.preference)
        preference_proxy.prepare_schema(self.proxy)
        self.model_root = self.root / "source-models"
        run_dir = self.model_root / "runs" / "run_test"
        run_dir.mkdir(parents=True)
        (run_dir / "model.pkl").write_bytes(b"deliberately-not-pickle: do not execute")
        manifest = {"run_id": "run_test", "model_revision": "local@one", "training_examples": 1,
            "training_source": "proxy:teacher_test:selective", "labels": [{"example_id": "teacher_test:1"}],
            "dependency_versions": {"scikit_learn": "fixture"},
            "artifacts": {"model.pkl": digest(run_dir / "model.pkl")}}
        (run_dir / "manifest.json").write_text(json.dumps(manifest))
        with sqlite3.connect(self.preference) as con:
            con.execute("INSERT INTO preference_model_runs VALUES(?,?,?,?,?,?,?)", (
                "run_test", "2026", "local@one", "text-v1", 1, json.dumps(manifest), str(run_dir)))
            con.execute("INSERT INTO preference_state VALUES('champion_run_id','run_test')")
            con.execute("CREATE TABLE user_feedback(secret TEXT)")
            con.execute("INSERT INTO user_feedback VALUES('MUST_NOT_COPY')")
        with sqlite3.connect(self.proxy) as con:
            con.execute("INSERT INTO proxy_profiles VALUES('profile','v1','{}','2026')")
            con.execute("INSERT INTO proxy_runs VALUES('teacher_test','profile','v1','v1','teacher',1,1,1,'source','complete','2026','2026')")
            for i, split in ((1, "training"), (2, "audit")):
                con.execute("""INSERT INTO proxy_queue(queue_id,run_id,position,split,stratum,
                    family_id,ats,job_id,company_snapshot,title_snapshot,description_snapshot,
                    semantic_text,metadata_json,template_cluster_id,leakage_group_id,source_fingerprint,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    i, "teacher_test", i, split, "uniform", "family" + str(i), "ashby", str(i),
                    "Example", "Engineer", "Build things", "Engineer. Build things.", "{}", "template", "leak", "source", "complete"))
                con.execute("INSERT INTO proxy_predictions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    i, "teacher@one", "v1", "{}", "{}", "{}", "{}", "{}", 1.0, "2026"))
            con.execute("INSERT INTO proxy_students VALUES(?,?,?,?,?,?,?,?,?)", (
                "teacher_test", "selective", "run_test", "local@one", 1, str(self.preference), str(self.model_root), "{}", "2026"))
        self.archive = self.root / "seed.tar"
        self.destination = self.root / "new-installation"

    def tearDown(self):
        self.tmp.cleanup()

    def export(self):
        return export_seed(career_db=self.career, standard_dir=self.standard,
            preference_db=self.preference, proxy_db=self.proxy,
            model_root=self.model_root, run_ids=["run_test"], output=self.archive)

    def load(self, identity="remote@two", expected=None):
        return import_seed(archive=self.archive, destination=self.destination,
            expected_sha256=expected or digest(self.archive), embedding_identity=identity)

    def test_selective_roundtrip_preserves_facts_training_and_inactive_models(self):
        # SQLite's transaction context does not close a connection. Fixture
        # writers can be collected during export, checkpointing committed WAL
        # pages into the main file without changing any database content. Flush
        # those pages before comparing physical source bytes.
        for path in (self.career, self.preference, self.proxy):
            with closing(sqlite3.connect(path)) as con:
                self.assertEqual(con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
        before = {p: digest(p) for p in (self.career, self.preference, self.proxy)}
        result = self.export()
        receipt = self.load()
        self.assertEqual(result["sha256"], digest(self.archive))
        self.assertEqual(receipt["models"][0]["status"], "migration_required")
        self.assertFalse(receipt["champion_active"])
        self.assertFalse(receipt["standard_registered"])
        imported = CareerStore(self.destination / "resume-lab.db").get_profile()
        self.assertEqual(imported["draft"]["content"], self.revision["content"])
        self.assertIsNone(imported["approved_revision_id"])
        with sqlite3.connect(self.destination / "job-boards-proxy.db") as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM proxy_queue").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM proxy_predictions").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT state_db FROM proxy_students").fetchone()[0], str(self.destination / "job-boards-preference.db"))
        with sqlite3.connect(self.destination / "job-boards-preference.db") as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM preference_state").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM preference_scores").fetchone()[0], 0)
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='user_feedback'").fetchone())
        self.assertEqual(before, {p: digest(p) for p in before})
        for path in self.destination.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(seed_status(self.destination), receipt)
        self.assertEqual((self.destination / ".models/preference/runs/run_test/model.pkl").read_bytes(), b"deliberately-not-pickle: do not execute")

    def test_live_export_reads_committed_wal_without_modifying_source_files(self):
        # Keep one writer alive so its last-close checkpoint cannot occur during
        # this check. The exporter must read facts present only in the WAL, while
        # leaving both source files byte-for-byte unchanged.
        with closing(sqlite3.connect(self.career)) as writer:
            self.assertEqual(writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
            checkpointed = digest(self.career)
            content = json.loads(json.dumps(self.revision["content"]))
            content["identity"]["name"] = "WAL-only Person"
            self.store.save_draft(content, expected_revision_id=self.revision["revision_id"])
            self.assertEqual(digest(self.career), checkpointed)
            wal = Path(str(self.career) + "-wal")
            self.assertGreater(wal.stat().st_size, 32)
            before = {path: digest(path) for path in (self.career, wal)}
            self.export()
            self.assertEqual(before, {path: digest(path) for path in before})
            with tarfile.open(self.archive) as archive:
                profile = json.loads(archive.extractfile("career.json").read())
            self.assertEqual(profile["current"]["content"]["identity"]["name"], "WAL-only Person")

    def test_repeated_import_never_overwrites_live_data(self):
        self.export()
        self.load()
        marker = self.destination / "live-user-state"
        marker.write_bytes(b"preserve")
        before = digest(self.destination / "resume-lab.db")
        with self.assertRaises(SeedError):
            self.load()
        self.assertEqual(marker.read_bytes(), b"preserve")
        self.assertEqual(digest(self.destination / "resume-lab.db"), before)

    def test_optional_teacher_provider_provenance_survives_import(self):
        with sqlite3.connect(self.proxy) as con:
            con.execute("ALTER TABLE proxy_runs ADD COLUMN teacher_provider TEXT NOT NULL DEFAULT ''")
            con.execute("UPDATE proxy_runs SET teacher_provider='fireworks'")
        self.export()
        receipt = self.load()
        with sqlite3.connect(self.destination / "job-boards-proxy.db") as con:
            self.assertEqual(con.execute("SELECT teacher_provider FROM proxy_runs").fetchone(), ("fireworks",))
        self.assertFalse(receipt["champion_active"])

    def test_unknown_teacher_columns_still_reject_import_without_publication(self):
        with sqlite3.connect(self.proxy) as con:
            con.execute("ALTER TABLE proxy_runs ADD COLUMN unexpected_column TEXT")
        self.export()
        with self.assertRaisesRegex(SeedError, "schema mismatch"):
            self.load()
        self.assertFalse(self.destination.exists())

    def test_existing_source_approval_does_not_attest_new_import(self):
        self.store.approve_revision(self.revision["revision_id"])
        self.export()
        receipt = self.load(identity="local@one")
        profile = CareerStore(self.destination / "resume-lab.db").get_profile()
        self.assertIsNone(profile["approved_revision_id"])
        self.assertEqual(profile["draft"]["provenance"]["source_approved_revision_id"], self.revision["revision_id"])
        self.assertEqual(receipt["models"][0]["status"], "imported_inactive")

    def test_runtime_root_rebases_registry_without_writing_outside_staging(self):
        self.export()
        runtime = Path("/var/lib/job-search")
        receipt = import_seed(archive=self.archive, destination=self.destination,
            expected_sha256=digest(self.archive), embedding_identity="remote@two", runtime_root=runtime)
        self.assertEqual(receipt["runtime_root"], str(runtime))
        with sqlite3.connect(self.destination / "job-boards-preference.db") as con:
            self.assertEqual(con.execute("SELECT artifact_path FROM preference_model_runs").fetchone()[0],
                "/var/lib/job-search/.models/preference/runs/run_test")
        with sqlite3.connect(self.destination / "job-boards-proxy.db") as con:
            row = con.execute("SELECT state_db,artifact_dir FROM proxy_students").fetchone()
            self.assertEqual(row, ("/var/lib/job-search/job-boards-preference.db", "/var/lib/job-search/.models/preference"))
        self.assertTrue((self.destination / ".models/preference/runs/run_test/model.pkl").exists())

    def test_runtime_root_rejects_ambiguous_paths(self):
        self.export()
        for target in (Path("relative"), Path("/"), Path("/var/../etc")):
            with self.assertRaises(SeedError):
                import_seed(archive=self.archive, destination=self.destination,
                    expected_sha256=digest(self.archive), embedding_identity="remote@two", runtime_root=target)
            self.assertFalse(self.destination.exists())

    def test_registration_preserves_existing_active_standard(self):
        ResumeLabService(self.career).create_standard("Standard", 1,
            StandardVersionInput("Example resume", "Example resume",
                (ResumeClaim("fact_test", "Example resume", ClaimOrigin.USER_ATTESTED),)), actor_kind="user")
        self.export()
        self.assertTrue(self.load()["standard_registered"])
        standards = ResumeLabService(self.destination / "resume-lab.db").list_active_standards()
        self.assertEqual([s["name"] for s in standards], ["Standard"])

    def test_corrupted_archive_hash_rejected_without_destination(self):
        self.export()
        with self.assertRaises(SeedError):
            self.load(expected="0" * 64)
        self.assertFalse(self.destination.exists())

    def test_member_checksum_rejected_even_when_archive_digest_matches(self):
        self.export()
        with tarfile.open(self.archive) as stream:
            members = [(item, stream.extractfile(item).read()) for item in stream]
        with tarfile.open(self.archive, "w") as stream:
            for item, raw in members:
                if item.name == "career.json":
                    raw = raw.replace(b"Example Person", b"Changed Person")
                    item.size = len(raw)
                stream.addfile(item, io.BytesIO(raw))
        with self.assertRaises(SeedError):
            self.load()
        self.assertFalse(self.destination.exists())

    def test_allow_empty_destination_is_explicit_and_never_overwrites(self):
        self.export()
        self.destination.mkdir(mode=0o700)
        with self.assertRaises(SeedError):
            self.load()
        result = import_seed(archive=self.archive, destination=self.destination,
            expected_sha256=digest(self.archive), embedding_identity="remote@two", allow_empty_destination=True)
        self.assertEqual(result["status"], "imported")
        with self.assertRaises(SeedError):
            import_seed(archive=self.archive, destination=self.destination,
                expected_sha256=digest(self.archive), embedding_identity="remote@two", allow_empty_destination=True)

    def test_offline_source_mode_refuses_wal_files(self):
        Path(str(self.career) + "-wal").touch()
        with self.assertRaises(SeedError):
            export_seed(career_db=self.career, standard_dir=self.standard,
                preference_db=self.preference, proxy_db=self.proxy,
                model_root=self.model_root, run_ids=["run_test"], output=self.archive, offline_sources=True)

    def test_artifact_corruption_fails_export(self):
        (self.model_root / "runs/run_test/model.pkl").write_bytes(b"corrupted")
        with self.assertRaises(SeedError):
            self.export()
        self.assertFalse(self.archive.exists())

    def test_archive_path_traversal_and_symlink_are_rejected(self):
        for link in (False, True):
            with tarfile.open(self.archive, "w") as tar:
                member = tarfile.TarInfo("linked" if link else "../outside")
                member.mode = 0o600
                if link:
                    member.type, member.linkname = tarfile.SYMTYPE, "/outside"
                    tar.addfile(member)
                else:
                    member.size = 1
                    tar.addfile(member, io.BytesIO(b"x"))
            self.archive.chmod(0o600)
            with self.assertRaises(SeedError):
                self.load()
            self.assertFalse(self.destination.exists())

    def test_archive_must_be_private_and_source_cannot_be_symlink(self):
        self.export()
        self.archive.chmod(0o644)
        with self.assertRaises(SeedError):
            self.load()
        self.archive.unlink()
        original = self.standard / STANDARD_FILES[0]
        renamed = self.standard / "elsewhere.pdf"
        original.rename(renamed)
        original.symlink_to(renamed)
        with self.assertRaises(SeedError):
            self.export()

    def test_teacher_queue_must_be_complete(self):
        with sqlite3.connect(self.proxy) as con:
            con.execute("UPDATE proxy_queue SET status='pending' WHERE queue_id=2")
        with self.assertRaises(SeedError):
            self.export()


if __name__ == "__main__":
    unittest.main()
