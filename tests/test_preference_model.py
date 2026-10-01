#!/usr/bin/env python3
"""Dependency-free offline tests for preference_model.py."""

from __future__ import annotations

import json
import contextlib
import io
import pickle
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace

from job_search.ranking import model as preference_module
from job_search.collection.dedupe import prepare_families

from job_search.ranking.model import (
    DependencyError,
    FeatureDocument,
    HashingEncoder,
    PreferenceModelError,
    TrainingExample,
    apply_baseline_selection_guard,
    build_feature_document,
    batched_neighbor_scores_and_liked,
    canonical_json,
    choose_fold_count,
    combined_cv_groups,
    default_state_db,
    decode_vector_blob,
    description_chunks,
    embed_documents,
    embedding_switch_gate,
    encoder_for_recorded_revision,
    encode_vector_blob,
    ensure_evaluation_embeddings,
    estimate_embedding_work,
    evaluate_model,
    group_evaluation_slices,
    load_combined_vectors,
    load_family_documents,
    load_evaluation_examples,
    load_training_examples,
    make_encoder,
    prepare_state,
    protected_example_set_fingerprint,
    main as preference_main,
    promote_champion,
    promote_run,
    require_module,
    refresh_pipeline,
    rollback_run,
    select_simplest_within,
    sha256_text,
    sha256_bytes,
    train_model,
    validate_group_splits,
)


def _source_db(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.executescript("""
        CREATE TABLE jobs (
            ats TEXT NOT NULL, id TEXT NOT NULL, company TEXT, title TEXT,
            department TEXT, team TEXT, employmentType TEXT, location TEXT,
            isRemote TEXT, workplaceType TEXT, publishedAt TEXT, jobUrl TEXT,
            description TEXT, matched TEXT, first_seen TEXT, last_seen TEXT,
            closed_at TEXT, PRIMARY KEY (ats,id)
        );
        CREATE TABLE job_families (
            family_id TEXT PRIMARY KEY, family_fingerprint TEXT UNIQUE,
            normalization_version INTEGER, ats TEXT, company_normalized TEXT,
            title_normalized TEXT, description_fingerprint TEXT,
            canonical_ats TEXT, canonical_job_id TEXT, member_count INTEGER
        );
        CREATE TABLE job_family_members (
            ats TEXT, job_id TEXT, family_id TEXT, source_fingerprint TEXT,
            PRIMARY KEY (ats,job_id)
        );
        CREATE TABLE job_template_clusters (
            family_id TEXT PRIMARY KEY, template_cluster_id TEXT,
            leakage_group_id TEXT, normalization_version INTEGER
        );
        INSERT INTO jobs VALUES (
            'ashby','1','Secret Company','Distributed Systems Engineer',
            'Infrastructure','Storage','FullTime','Secret City','false','OnSite',
            '2026-08-01','https://example.test/1',
            'Design storage engines.\n\nImprove replication and reliability.',
            '', '2026-08-01','2026-08-01',NULL
        );
        INSERT INTO job_families VALUES (
            'fam_1','fp1',1,'ashby','secret company','distributed systems engineer',
            'dfp1','ashby','1',1
        );
        INSERT INTO job_family_members VALUES ('ashby','1','fam_1','source1');
        INSERT INTO job_template_clusters VALUES ('fam_1','tpl_1','leak_1',1);
        """)


def _snapshot_schema(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.executescript("""
        CREATE TABLE job_preferences (
            ats TEXT, job_id TEXT, interest TEXT, sample_role TEXT,
            hard_blockers TEXT, updated_at TEXT, current_example_id INTEGER,
            PRIMARY KEY (ats,job_id)
        );
        CREATE TABLE preference_examples (
            example_id INTEGER PRIMARY KEY, ats TEXT, job_id TEXT,
            family_id TEXT, interest TEXT,
            sample_role TEXT, title_snapshot TEXT, description_snapshot TEXT,
            metadata_json TEXT, template_cluster_id TEXT, leakage_group_id TEXT,
            hard_blockers TEXT, dataset_version TEXT, source_fingerprint TEXT,
            selection_strategy TEXT, selection_probability REAL
        );
        CREATE VIEW preference_training_examples AS
        SELECT e.* FROM preference_examples e
        JOIN job_preferences p ON p.current_example_id=e.example_id
        WHERE e.sample_role='training'
          AND e.interest IN ('interested','not_interested');
        CREATE VIEW preference_evaluation_examples AS
        SELECT e.* FROM preference_examples e
        JOIN job_preferences p ON p.current_example_id=e.example_id
        WHERE e.sample_role='evaluation'
          AND e.interest IN ('interested','not_interested');
        """)


def _insert_example(
    con: sqlite3.Connection,
    example_id: int,
    job_id: str,
    family_id: str,
    interest: str,
    role: str,
    blockers: str = "[]",
    strategy: str = "uniform",
) -> None:
    con.execute(
        "INSERT INTO preference_examples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            example_id, "ashby", job_id, family_id, interest, role, f"Title {job_id}",
            f"Description {job_id}", '{"department":"Infra","team":"Core"}',
            f"tpl_{family_id}", f"leak_{family_id}", blockers,
            "preference-v1", f"source-{job_id}", strategy, 0.5,
        ),
    )
    con.execute(
        "INSERT INTO job_preferences VALUES (?,?,?,?,?,?,?)",
        ("ashby", job_id, interest, role, blockers, "2026-08-01", example_id),
    )


def test_feature_allowlist_excludes_constraints_and_identity() -> None:
    base = {
        "family_id": "fam_1", "title": "ML Engineer", "department": "AI",
        "team": "Ranking", "description": "Build preference models.",
        "template_cluster_id": "tpl", "leakage_group_id": "leak",
        "company": "A", "salary": "$1", "location": "Chicago", "ats": "ashby",
        "employmentType": "contract", "hard_blockers": '["location"]',
    }
    changed = dict(base)
    changed.update({
        "company": "B", "salary": "$999999", "location": "Remote",
        "ats": "lever", "employmentType": "full-time", "hard_blockers": "[]",
    })
    left = build_feature_document(base)
    right = build_feature_document(changed)
    assert left.title_metadata_text == right.title_metadata_text
    assert left.description_text == right.description_text
    assert left.fingerprint == right.fingerprint
    assert "company" not in left.word_text.casefold()
    assert "chicago" not in left.word_text.casefold()


def test_chunking_is_bounded_paragraph_aligned_and_spans_posting() -> None:
    paragraphs = [" ".join(f"p{i}word{j}" for j in range(9)) for i in range(8)]
    chunks = description_chunks("\n\n".join(paragraphs), max_chunks=4, max_tokens=5)
    assert len(chunks) == 4
    assert all(len(chunk.split()) <= 5 for chunk in chunks)
    assert chunks[0].startswith("p0word0")
    assert chunks[-1].endswith("p7word8")
    middle_paragraphs = {
        int(match[1:])
        for chunk in chunks[1:-1]
        for match in [chunk.split("word", 1)[0]]
        if match.startswith("p")
    }
    assert middle_paragraphs and min(middle_paragraphs) > 0 and max(middle_paragraphs) < 7


def test_embedding_cache_reuses_text_and_invalidates_only_changed_document() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "preference.db"
        encoder = HashingEncoder(16)
        first = build_feature_document({
            "family_id": "fam_1", "title": "Engineer", "department": "Infra",
            "team": "Core", "description": "Build systems.",
        })
        result = embed_documents(state, [first], encoder)
        assert result["embedded_texts"] == 2
        assert embed_documents(state, [first], encoder)["embedded_texts"] == 0

        forbidden_only = build_feature_document({
            "family_id": "fam_1", "title": "Engineer", "department": "Infra",
            "team": "Core", "description": "Build systems.", "company": "Changed",
            "location": "Moon", "salary": "millions",
        })
        assert forbidden_only.fingerprint == first.fingerprint
        assert embed_documents(state, [forbidden_only], encoder)["embedded_texts"] == 0

        changed = build_feature_document({
            "family_id": "fam_1", "title": "Engineer", "department": "Infra",
            "team": "Core", "description": "Build reliable systems.",
        })
        assert embed_documents(state, [changed], encoder)["embedded_texts"] == 1
        vectors = load_combined_vectors(state, [changed], encoder.model_revision)
        assert len(vectors) == 1 and len(vectors[0]) == 32

        with sqlite3.connect(state) as con:
            cache_count = con.execute(
                "SELECT COUNT(*) FROM preference_embedding_cache"
            ).fetchone()[0]
            storage = con.execute(
                "SELECT DISTINCT typeof(vector_blob),length(vector_blob),dimensions "
                "FROM preference_embedding_cache"
            ).fetchall()
            feature_state = con.execute(
                "SELECT title_metadata_text,description_text,description_chunks_json "
                "FROM preference_feature_documents WHERE subject_id='fam_1'"
            ).fetchone()
        assert cache_count == 3
        assert storage == [("blob", 64, 16)]
        assert feature_state == ("", "", "[]")
        assert "Build reliable systems" not in str(feature_state)


def test_float32_blob_roundtrip_is_little_endian_and_deterministic() -> None:
    vector = [1.0, -2.5, 0.125]
    first = encode_vector_blob(vector)
    second = encode_vector_blob(vector)
    assert first == second
    assert first.hex() == "0000803f000020c00000003e"
    assert decode_vector_blob(first, 3) == vector
    try:
        decode_vector_blob(first[:-1], 3)
    except ValueError as exc:
        assert "expected 12" in str(exc)
    else:
        raise AssertionError("expected malformed BLOB rejection")


def test_json_embedding_cache_migrates_to_float32_blob() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.db"
        with sqlite3.connect(state) as con:
            con.execute("""
                CREATE TABLE preference_embedding_cache (
                    model_revision TEXT, text_version TEXT, text_fingerprint TEXT,
                    vector_json TEXT, dimensions INTEGER, created_at TEXT,
                    PRIMARY KEY(model_revision,text_version,text_fingerprint)
                )
            """)
            con.execute(
                "INSERT INTO preference_embedding_cache VALUES (?,?,?,?,?,?)",
                ("model@abc", "v1", "fp", "[1.0,-2.5,0.125]", 3, "now"),
            )
        prepare_state(state)
        with sqlite3.connect(state) as con:
            con.row_factory = sqlite3.Row
            columns = {
                row[1] for row in con.execute("PRAGMA table_info(preference_embedding_cache)")
            }
            row = con.execute("SELECT * FROM preference_embedding_cache").fetchone()
        assert "vector_blob" in columns and "vector_json" not in columns
        assert decode_vector_blob(row["vector_blob"], row["dimensions"]) == [1.0, -2.5, 0.125]


def test_identical_documents_share_cached_vectors() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "preference.db"
        encoder = HashingEncoder(8)
        docs = [
            build_feature_document({
                "family_id": family, "title": "Engineer", "department": "AI",
                "description": "Train ranking models.",
            })
            for family in ("fam_a", "fam_b")
        ]
        result = embed_documents(state, docs, encoder)
        assert result == {"documents": 2, "unique_texts": 2, "embedded_texts": 2}


def test_embedding_encoder_calls_are_strictly_bounded() -> None:
    class RecordingEncoder:
        model_revision = "recording-v1"

        def __init__(self) -> None:
            self.batch_sizes: list[int] = []
            self.delegate = HashingEncoder(8)

        def encode(self, texts: list[str]) -> list[list[float]]:
            self.batch_sizes.append(len(texts))
            return self.delegate.encode(texts)

    documents = [
        build_feature_document({
            "family_id": f"fam_{index}", "title": f"Engineer {index}",
            "description": f"Build distinct system {index}.",
        })
        for index in range(10)
    ]
    with tempfile.TemporaryDirectory() as tmp:
        encoder = RecordingEncoder()
        result = embed_documents(
            Path(tmp) / "state.db", iter(documents), encoder, batch_size=3,
        )
    assert result["documents"] == 10
    assert result["embedded_texts"] == 20
    assert len(encoder.batch_sizes) > 1
    assert max(encoder.batch_sizes) <= 3


def test_provider_batch_checkpoint_survives_later_deferral():
    class Deferred(RuntimeError):
        pass

    class StreamingEncoder:
        model_revision = "streaming-fixture-v1"
        fail = True

        def __init__(self):
            self.calls = []

        def iter_encode_batches(self, texts):
            for index, text in enumerate(texts):
                if self.fail and index == 1:
                    raise Deferred("daily request limit")
                self.calls.append(text)
                yield [text], HashingEncoder(8).encode([text])

    document = build_feature_document({"family_id": "checkpoint-1", "title": "Engineer", "description": "Build reliable systems."})
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory) / "preference.db"
        encoder = StreamingEncoder()
        try:
            embed_documents(state, [document], encoder)
            raise AssertionError("expected provider deferral")
        except Deferred:
            pass
        with sqlite3.connect(state) as con:
            assert con.execute("SELECT COUNT(*) FROM preference_embedding_cache").fetchone()[0] == 1
        first_text = encoder.calls[0]
        encoder.fail = False
        result = embed_documents(state, [document], encoder)
        assert result["embedded_texts"] == 1
        assert encoder.calls.count(first_text) == 1
        assert embed_documents(state, [document], encoder)["embedded_texts"] == 0


def test_provider_batch_cannot_bind_vectors_to_different_text():
    class WrongEncoder:
        model_revision = "wrong-stream-v1"

        def iter_encode_batches(self, texts):
            yield ["another text"], [[1.0, 0.0]]

    document = build_feature_document({"family_id": "checkpoint-2", "title": "Engineer", "description": "Build systems."})
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory) / "preference.db"
        try:
            embed_documents(state, [document], WrongEncoder())
            raise AssertionError("expected invalid batch rejection")
        except PreferenceModelError:
            pass
        with sqlite3.connect(state) as con:
            assert con.execute("SELECT COUNT(*) FROM preference_embedding_cache").fetchone()[0] == 0


def test_remote_encoder_requires_exact_revision_and_records_provenance() -> None:
    class FakeRemoteEncoder:
        model_revision = "BAAI/bge-base-en-v1.5@immutable-test-revision"
        provenance = {
            "provider": "test-cloud",
            "protocol": "openai-compatible-embeddings",
            "model_revision": model_revision,
            "deployment_revision": "worker@immutable-test-revision",
        }

        def encode(self, texts):
            return HashingEncoder(8).encode(texts)

    remote = FakeRemoteEncoder()
    encoder = make_encoder(
        "remote", "ignored", None, "auto", remote_provider=remote,
    )
    assert encoder is remote
    assert encoder_for_recorded_revision(
        remote.model_revision, remote_provider=remote,
    ) is remote
    try:
        encoder_for_recorded_revision(
            "BAAI/bge-base-en-v1.5@different", remote_provider=remote,
        )
    except PreferenceModelError as exc:
        assert "does not match artifact revision" in str(exc)
    else:
        raise AssertionError("expected exact remote-revision rejection")

    document = build_feature_document({
        "family_id": "remote-family", "title": "Remote Encoder Engineer",
        "description": "Build portable inference systems.",
    })
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory) / "state.db"
        embed_documents(state, [document], remote)
        with sqlite3.connect(state) as con:
            recorded = con.execute(
                "SELECT value FROM preference_state "
                "WHERE key='embedding_provider_provenance'"
            ).fetchone()[0]
            history = con.execute(
                "SELECT provenance_json FROM preference_embedding_provenance"
            ).fetchone()[0]
        assert json.loads(recorded) == remote.provenance
        assert json.loads(history) == remote.provenance


def test_dependency_free_neighbor_scoring_is_batched_and_explained() -> None:
    scores, liked = batched_neighbor_scores_and_liked(
        train_vectors=[[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]],
        train_targets=[1, 0, 1],
        train_family_ids=["liked-near", "pass", "liked-far"],
        test_vectors=iter([[1.0, 0.0], [0.0, 1.0]]),
        k=2,
        batch_size=1,
    )
    assert scores == [0.75, 0.25]
    assert liked == [
        ["liked-near", "liked-far"],
        ["liked-near", "liked-far"],
    ]


def test_evaluation_preflight_embeds_only_snapshots_without_changing_active_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        state = Path(tmp) / "state.db"
        _source_db(source)
        prepare_state(state)
        with sqlite3.connect(state) as con:
            con.execute(
                "INSERT INTO preference_state VALUES ('embedding_model_revision','other@1')"
            )
        document = build_feature_document({
            "family_id": "eval-family", "title": "Evaluation Engineer",
            "description": "Evaluate ranking quality.",
        }, "example", "eval-1")
        revision = HashingEncoder(8).model_revision
        result = ensure_evaluation_embeddings(
            source, state, [document], revision,
        )
        assert result["documents"] == 1 and result["embedded_texts"] == 2
        assert load_combined_vectors(state, [document], revision)
        with sqlite3.connect(state) as con:
            active = con.execute(
                "SELECT value FROM preference_state WHERE key='embedding_model_revision'"
            ).fetchone()[0]
        assert active == "other@1"


def test_evaluation_preflight_never_substitutes_a_different_encoder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        state = Path(tmp) / "state.db"
        _source_db(source)
        document = build_feature_document({
            "family_id": "eval-family", "title": "Evaluation Engineer",
            "description": "Evaluate ranking quality.",
        }, "example", "eval-1")
        try:
            ensure_evaluation_embeddings(
                source, state, [document], "BAAI/bge-base-en-v1.5@deadbeefdeadbeefdead",
            )
        except PreferenceModelError as exc:
            message = str(exc)
            assert "need exact BAAI/bge-base-en-v1.5@deadbeefdeadbeefdead" in message
            assert "--model BAAI/bge-base-en-v1.5" in message
            assert "--model-revision deadbeefdeadbeefdead" in message
        else:
            raise AssertionError("expected exact-encoder preflight error")


def test_source_family_contract_and_state_database_are_separate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        state = default_state_db(source)
        _source_db(source)
        docs = load_family_documents(source)
        assert len(docs) == 1
        assert docs[0].family_id == "fam_1"
        assert docs[0].template_cluster_id == "tpl_1"
        assert docs[0].leakage_group_id == "leak_1"
        embed_documents(state, docs, HashingEncoder(8))
        with sqlite3.connect(source) as con:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert "preference_embedding_cache" not in tables
        assert state.name == "jobs-preference.db"


def test_cli_refuses_to_put_model_state_in_operational_database() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        _source_db(source)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            status = preference_main([
                "embed", "--db", str(source), "--state-db", str(source),
                "--encoder", "hashing",
            ])
        assert status == 2
        assert "must differ" in errors.getvalue()
        with sqlite3.connect(source) as con:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert "preference_embedding_cache" not in tables


def test_cli_hashing_encoder_runs_offline_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        _source_db(source)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = preference_main(["embed", "--db", str(source), "--encoder", "hashing"])
        assert status == 0
        result = json.loads(output.getvalue())
        assert result["documents"] == 1
        assert result["model_revision"].startswith("hashing-test-v1")
        assert default_state_db(source).exists()


def test_non_refresh_commands_reject_jobs_without_families() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        _source_db(source)
        with sqlite3.connect(source) as con:
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "lever", "2", "New Co", "New Engineer", "Engineering", "",
                    "FullTime", "Remote", "true", "Remote", "2026-09-01",
                    "https://example.test/2", "Build a newly scraped service.", "",
                    "2026-09-01", "2026-09-01", None,
                ),
            )
        try:
            load_family_documents(source)
        except PreferenceModelError as exc:
            assert "run job_dedupe prepare or refresh" in str(exc)
        else:
            raise AssertionError("expected stale-family rejection")


def test_refresh_prepares_new_jobs_before_embedding() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        _source_db(source)
        with sqlite3.connect(source) as con:
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "lever", "2", "New Co", "New Engineer", "Engineering", "",
                    "FullTime", "Remote", "true", "Remote", "2026-09-01",
                    "https://example.test/2", "Build a newly scraped service.", "",
                    "2026-09-01", "2026-09-01", None,
                ),
            )
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            status = preference_main([
                "refresh", "--db", str(source), "--encoder", "hashing",
            ])
        # Dedupe and embedding complete before the expected no-label train stop.
        assert status == 2
        assert "no labels" in errors.getvalue()
        with sqlite3.connect(source) as con:
            assert con.execute("SELECT COUNT(*) FROM job_family_members").fetchone()[0] == 2
        with sqlite3.connect(default_state_db(source)) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM preference_embedding_refs WHERE subject_type='family'"
            ).fetchone()[0] == 2


def test_estimate_reports_raw_family_vector_volume_without_state_or_encoder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        _source_db(source)
        estimate = estimate_embedding_work(source, dimensions=3, limit=1)
        assert estimate == {
            "families": 1,
            "title_inputs": 1,
            "chunk_inputs": 2,
            "total_encoder_inputs": 3,
            "dimensions": 3,
            "bytes_per_vector": 12,
            "raw_vector_bytes": 36,
            "raw_vector_mib": 0.0,
            "limited": True,
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = preference_main([
                "estimate", "--db", str(source), "--dimensions", "3", "--limit", "1",
            ])
        assert status == 0
        assert json.loads(output.getvalue()) == estimate
        assert not default_state_db(source).exists()


def test_refresh_uses_exact_champion_encoder_and_scores_champion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        state = Path(tmp) / "state.db"
        artifacts = Path(tmp) / "artifacts"
        _source_db(source)
        _snapshot_schema(source)
        with sqlite3.connect(source) as con:
            _insert_example(
                con, 1, "1", "fam_1", "interested", "training",
            )
        prepare_state(state)
        _seed_candidate_run(
            state, artifacts, "run_hash", "hashing-test-v1-8", 0.5, 0.5,
        )
        promote_run(state, artifacts, "run_hash", "initial smoke champion")
        calls: list[str] = []
        original_score = preference_module.score_model

        def fake_score(source_db: Path, state_db: Path, run_id: str | None = None) -> dict:
            del source_db, state_db
            calls.append(str(run_id))
            return {"run_id": run_id, "scored_families": 1}

        preference_module.score_model = fake_score
        try:
            result = refresh_pipeline(SimpleNamespace(
                db=source, device="auto", min_training_labels=200,
                retrain_every_labels=25, encoder="sentence-transformers",
                model="BAAI/bge-base-en-v1.5", model_revision=None,
            ), state, artifacts)
        finally:
            preference_module.score_model = original_score
        assert calls == ["run_hash"]
        assert result["champion_run_id"] == "run_hash"
        assert result["candidate"] is None
        assert result["candidate_cadence"]["status"] == "awaiting_more_labels"
        with sqlite3.connect(state) as con:
            active = con.execute(
                "SELECT value FROM preference_state WHERE key='embedding_model_revision'"
            ).fetchone()[0]
        assert active == "hashing-test-v1-8"


def test_training_uses_only_current_decisive_training_snapshots() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        _source_db(source)
        _snapshot_schema(source)
        with sqlite3.connect(source) as con:
            _insert_example(con, 1, "train-like", "fam_like", "interested", "training", '["location"]')
            _insert_example(con, 2, "train-pass", "fam_pass", "not_interested", "training")
            _insert_example(con, 3, "maybe", "fam_maybe", "maybe", "training")
            _insert_example(
                con, 4, "eval", "fam_eval", "interested", "evaluation",
                strategy="protected_uniform",
            )
            _insert_example(con, 5, "skip", "fam_skip", "skipped", "training")
        examples = load_training_examples(source)
        assert [(example.example_id, example.target) for example in examples] == [
            ("1", 1), ("2", 0),
        ]
        # A blocker is an independent feasibility annotation; it is not converted
        # into a negative intrinsic-interest target or included in feature text.
        assert examples[0].target == 1
        assert "location" not in examples[0].document.word_text
        evaluation = load_evaluation_examples(source)
        assert [(example.example_id, example.selection_strategy) for example in evaluation] == [
            ("4", "protected_uniform"),
        ]
        try:
            evaluate_model(
                source, Path(tmp) / "state.db", Path(tmp) / "artifacts",
            )
        except PreferenceModelError as exc:
            assert "requires all 200" in str(exc)
        else:
            raise AssertionError("expected protected evaluation completion gate")
        try:
            train_model(source, Path(tmp) / "state.db", Path(tmp) / "artifacts")
        except PreferenceModelError as exc:
            assert "needs 200" in str(exc)
        else:
            raise AssertionError("expected provisional label threshold")


def test_conflicting_family_labels_are_excluded() -> None:
    document = build_feature_document({"family_id": "fam", "title": "Engineer"}, "example", "1")
    other = FeatureDocument(
        subject_type="example", subject_id="2", family_id="fam",
        title_metadata_text=document.title_metadata_text, description_text="",
        description_chunks=(), template_cluster_id="tpl", leakage_group_id="leak",
    )
    from job_search.ranking.model import _deduplicate_training_families
    assert _deduplicate_training_families([
        TrainingExample("1", document, 1), TrainingExample("2", other, 0),
    ]) == []


def test_group_validation_rejects_leakage() -> None:
    groups = ["a", "a", "b", "c"]
    validate_group_splits([([0, 1], [2, 3]), ([2, 3], [0, 1])], groups)
    try:
        validate_group_splits([([0, 2], [1, 3])], groups)
    except PreferenceModelError as exc:
        assert "cross a fold" in str(exc)
    else:
        raise AssertionError("expected leakage rejection")
    assert choose_fold_count([0] * 5 + [1] * 5, [str(i) for i in range(10)]) == 5
    assert choose_fold_count([0] * 3 + [1] * 3, [str(i) for i in range(6)]) == 3


def test_cv_groups_connect_template_and_exact_description_leakage() -> None:
    documents = [
        build_feature_document({
            "family_id": "a", "title": "A", "template_cluster_id": "template-1",
            "leakage_group_id": "leak-1",
        }),
        build_feature_document({
            "family_id": "b", "title": "B", "template_cluster_id": "template-1",
            "leakage_group_id": "leak-2",
        }),
        build_feature_document({
            "family_id": "c", "title": "C", "template_cluster_id": "template-2",
            "leakage_group_id": "leak-2",
        }),
        build_feature_document({
            "family_id": "d", "title": "D", "template_cluster_id": "template-3",
            "leakage_group_id": "leak-3",
        }),
    ]
    groups = combined_cv_groups(documents)
    assert groups[0] == groups[1] == groups[2]
    assert groups[3] != groups[0]


def test_template_lineage_connects_labels_across_two_prepare_cycles() -> None:
    """An evolving near-template component must remain one CV leakage group."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "jobs.db"
        _source_db(source)
        _snapshot_schema(source)
        first_text = " ".join(
            ["Build operate and improve reliable cloud systems for global customers."] * 30
        )
        second_text = " ".join(
            ["Create brand illustrations typography campaigns and visual design systems."] * 30
        )
        with sqlite3.connect(source) as con:
            con.execute(
                "UPDATE jobs SET company='Acme',title='Engineer',description=? "
                "WHERE ats='ashby' AND id='1'",
                (first_text,),
            )
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "ashby", "2", "Acme", "Engineer", "", "", "FullTime", "Remote",
                    "true", "Remote", "2026-08-02", "https://example.test/2",
                    second_text, "", "2026-08-02", "2026-08-02", None,
                ),
            )
        prepare_families(source)

        def context(con: sqlite3.Connection, job_id: str) -> sqlite3.Row:
            con.row_factory = sqlite3.Row
            return con.execute(
                "SELECT j.title,j.description,m.family_id,m.source_fingerprint,"
                "tc.template_cluster_id,tc.leakage_group_id FROM jobs j "
                "JOIN job_family_members m ON m.ats=j.ats AND m.job_id=j.id "
                "JOIN job_template_clusters tc ON tc.family_id=m.family_id "
                "WHERE j.ats='ashby' AND j.id=?",
                (job_id,),
            ).fetchone()

        with sqlite3.connect(source) as con:
            initial = {job_id: context(con, job_id) for job_id in ("1", "2")}
            assert len({row["template_cluster_id"] for row in initial.values()}) == 2
            labeled_first = max(
                initial, key=lambda job_id: initial[job_id]["template_cluster_id"]
            )
            other = "1" if labeled_first == "2" else "2"
            first = initial[labeled_first]
            _insert_example(
                con, 1, labeled_first, first["family_id"], "interested", "training",
            )
            con.execute(
                "UPDATE preference_examples SET title_snapshot=?,description_snapshot=?,"
                "template_cluster_id=?,leakage_group_id=?,source_fingerprint=? "
                "WHERE example_id=1",
                (
                    first["title"], first["description"], first["template_cluster_id"],
                    first["leakage_group_id"], first["source_fingerprint"],
                ),
            )
            # The same source posting changes into a near-template of the other family.
            con.execute(
                "UPDATE jobs SET description=? WHERE ats='ashby' AND id=?",
                (initial[other]["description"] + " additional", labeled_first),
            )

        prepare_families(source)
        with sqlite3.connect(source) as con:
            second = context(con, other)
            edited = context(con, labeled_first)
            assert second["template_cluster_id"] == edited["template_cluster_id"]
            assert first["template_cluster_id"] != second["template_cluster_id"]
            _insert_example(
                con, 2, other, second["family_id"], "not_interested", "training",
            )
            con.execute(
                "UPDATE preference_examples SET title_snapshot=?,description_snapshot=?,"
                "template_cluster_id=?,leakage_group_id=?,source_fingerprint=? "
                "WHERE example_id=2",
                (
                    second["title"], second["description"], second["template_cluster_id"],
                    second["leakage_group_id"], second["source_fingerprint"],
                ),
            )

        examples = load_training_examples(source)
        assert len(examples) == 2
        assert len({example.document.template_cluster_id for example in examples}) == 1
        assert len(set(combined_cv_groups([example.document for example in examples]))) == 1


def test_selection_chooses_simplest_model_within_one_point() -> None:
    selected = select_simplest_within([
        {"name": "ensemble", "ndcg_at_20": 0.800, "complexity": 3},
        {"name": "dense_linear", "ndcg_at_20": 0.792, "complexity": 1},
        {"name": "sparse", "ndcg_at_20": 0.770, "complexity": 1},
    ])
    assert selected["name"] == "dense_linear"


def test_baseline_guard_falls_back_to_strongest_single_component() -> None:
    candidates = [
        {"name": "ensemble", "ndcg_at_20": 0.70, "weights": {"dense_linear": 0.5, "sparse": 0.5}},
        {"name": "dense_linear", "ndcg_at_20": 0.68, "weights": {"dense_linear": 1.0}},
        {"name": "dense_neighbor", "ndcg_at_20": 0.66, "weights": {"dense_neighbor": 1.0}},
        {"name": "sparse", "ndcg_at_20": 0.69, "weights": {"sparse": 1.0}},
    ]
    selected, reason = apply_baseline_selection_guard(
        candidates[0], candidates,
        {
            "title_only": {"ndcg_at_20": 0.71},
            "random": {"ndcg_at_20": 0.50},
        },
    )
    assert selected["name"] == "sparse"
    assert reason == "baseline_fallback"
    selected, reason = apply_baseline_selection_guard(
        {**candidates[0], "ndcg_at_20": 0.72}, candidates,
        {
            "title_only": {"ndcg_at_20": 0.71},
            "random": {"ndcg_at_20": 0.50},
        },
    )
    assert selected["name"] == "ensemble"
    assert reason == "out_of_fold_winner"


def _perfect_tie_evidence() -> dict:
    return {
        "selected": {
            "name": "dense_linear", "weights": {"dense_linear": 1.0},
            "ndcg_at_20": 1.0, "precision_at_20": 1.0, "average_precision": 0.86,
        },
        "baselines": {
            "title_only": {
                "ndcg_at_20": 1.0, "precision_at_20": 1.0, "average_precision": 0.84,
            },
            "random": {"ndcg_at_20": 0.20, "average_precision": 0.32},
        },
    }


def test_perfect_top_twenty_tie_requires_broader_improvement() -> None:
    evidence = _perfect_tie_evidence()
    selected, reason = apply_baseline_selection_guard(
        evidence["selected"], [evidence["selected"]], evidence["baselines"],
    )
    assert selected == evidence["selected"]
    assert reason == "out_of_fold_perfect_score_tiebreak"
    assert preference_module._clears_training_baselines({"out_of_fold": evidence})

    # Near-perfect scores must not be rounded into the ceiling exception.
    for score in (0.7, 1.0 - 1e-12):
        ordinary = _perfect_tie_evidence()
        ordinary["selected"]["ndcg_at_20"] = score
        ordinary["baselines"]["title_only"]["ndcg_at_20"] = score
        assert not preference_module._clears_training_baselines({"out_of_fold": ordinary})

    for row_name, field, values in (
        ("selected", "precision_at_20", (0.95,)),
        ("title_only", "precision_at_20", (0.95,)),
        ("random", "ndcg_at_20", (1.0,)),
        ("selected", "average_precision", (0.84, 0.83)),
        ("random", "average_precision", (0.86, 0.87)),
    ):
        for value in values:
            rejected = _perfect_tie_evidence()
            row = rejected["selected"] if row_name == "selected" else rejected["baselines"][row_name]
            row[field] = value
            _, reason = apply_baseline_selection_guard(
                rejected["selected"], [rejected["selected"]], rejected["baselines"],
            )
            assert reason == "baseline_fallback", (row_name, field, value)
            assert not preference_module._clears_training_baselines({"out_of_fold": rejected})


def test_perfect_tie_rejects_missing_and_invalid_evidence() -> None:
    for row_name, fields in (
        ("selected", ("ndcg_at_20", "precision_at_20", "average_precision")),
        ("title_only", ("ndcg_at_20", "precision_at_20", "average_precision")),
        ("random", ("ndcg_at_20", "average_precision")),
    ):
        for field in fields:
            for value in (None, "invalid", float("nan"), float("inf"), -0.1, 1.1):
                evidence = _perfect_tie_evidence()
                row = evidence["selected"] if row_name == "selected" else evidence["baselines"][row_name]
                if value is None:
                    del row[field]
                else:
                    row[field] = value
                assert not preference_module._clears_training_baselines({"out_of_fold": evidence}), (
                    row_name, field, value,
                )


def test_protected_evaluation_slices_remain_separate() -> None:
    examples = []
    for index, strategy in enumerate((
        "protected_top_ranked", "protected_uniform", "protected_company_holdout",
    )):
        document = build_feature_document({
            "family_id": f"fam_{index}", "title": f"Role {index}",
        }, "example", str(index))
        examples.append(TrainingExample(
            str(index), document, index % 2, "preference-v1", "source", strategy, 1.0,
        ))
    grouped = group_evaluation_slices(examples)
    assert {name: len(values) for name, values in grouped.items()} == {
        "protected_top_ranked": 1,
        "protected_uniform": 1,
        "protected_company_holdout": 1,
    }


def test_missing_optional_dependency_has_actionable_error() -> None:
    def missing(name: str) -> None:
        raise ImportError(name)
    try:
        require_module("sklearn", "train", importer=missing)
    except DependencyError as exc:
        message = str(exc)
        assert "requirements/preference.txt" in message
        assert "sklearn" in message
    else:
        raise AssertionError("expected DependencyError")


def test_canonical_storage_and_champion_pointer_are_deterministic() -> None:
    assert canonical_json({"b": 1, "a": [2]}) == '{"a":[2],"b":1}'
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.db"
        artifacts = Path(tmp) / "artifacts"
        prepare_state(state)
        with sqlite3.connect(state) as con:
            con.execute(
                "INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)",
                ("run_x", "now", "model@abc", "text-v1", 6, "{}", str(artifacts)),
            )
        digest = sha256_text("manifest")
        promote_champion(state, artifacts, "run_x", digest)
        first = (artifacts / "champion.json").read_bytes()
        promote_champion(state, artifacts, "run_x", digest)
        second = (artifacts / "champion.json").read_bytes()
        assert first == second
        assert json.loads(first) == {"manifest_sha256": digest, "run_id": "run_x"}
        with sqlite3.connect(state) as con:
            assert con.execute(
                "SELECT value FROM preference_state WHERE key='champion_run_id'"
            ).fetchone()[0] == "run_x"


def test_model_artifact_hash_is_verified_before_unpickling() -> None:
    from job_search.ranking.model import _load_artifact

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.db"
        run_dir = Path(tmp) / "runs" / "run_verified"
        run_dir.mkdir(parents=True)
        model_bytes = pickle.dumps({"safe": True}, protocol=4)
        (run_dir / "model.pkl").write_bytes(model_bytes)
        manifest = {"artifacts": {"model.pkl": sha256_bytes(model_bytes)}}
        prepare_state(state)
        with sqlite3.connect(state) as con:
            con.execute(
                "INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)",
                (
                    "run_verified", "now", "model@abc", "v1", 200,
                    canonical_json(manifest), str(run_dir),
                ),
            )
        artifact, _ = _load_artifact(state, "run_verified")
        assert artifact == {"safe": True}
        (run_dir / "model.pkl").write_bytes(model_bytes + b"corrupt")
        try:
            _load_artifact(state, "run_verified")
        except PreferenceModelError as exc:
            assert "hash verification failed" in str(exc)
        else:
            raise AssertionError("expected artifact hash rejection")


def _seed_candidate_run(
    state: Path,
    artifacts: Path,
    run_id: str,
    model_revision: str,
    eval_ndcg: float,
    eval_precision: float,
    out_of_fold: dict | None = None,
) -> None:
    run_dir = artifacts / "runs" / run_id
    run_dir.mkdir(parents=True)
    model_bytes = pickle.dumps({"run_id": run_id}, protocol=4)
    (run_dir / "model.pkl").write_bytes(model_bytes)
    evaluation = {
        "example_ids": [str(index) for index in range(200)],
        "slices": {
            "protected_top_ranked": {
                "metrics": {
                    "ndcg_at_20": eval_ndcg,
                    "precision_at_20": eval_precision,
                },
            },
        },
    }
    manifest = {
        "artifacts": {"model.pkl": sha256_bytes(model_bytes)},
        "out_of_fold": out_of_fold if out_of_fold is not None else {
            "selected": {"ndcg_at_20": 0.80},
            "baselines": {
                "title_only": {"ndcg_at_20": 0.70},
                "random": {"ndcg_at_20": 0.50},
            },
        },
        "protected_evaluation": evaluation,
    }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    ).encode()
    (run_dir / "manifest.json").write_bytes(manifest_bytes)
    with sqlite3.connect(state) as con:
        con.execute(
            "INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)",
            (
                run_id, "now", model_revision, "v1", 200,
                canonical_json(manifest), str(run_dir),
            ),
        )


def test_perfect_tie_promotion_preserves_artifacts_and_cross_encoder_gates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.db"
        artifacts = Path(tmp) / "artifacts"
        prepare_state(state)
        _seed_candidate_run(state, artifacts, "run_tie", "bge@1", 0.50, 0.50, _perfect_tie_evidence())
        manifest = artifacts / "runs" / "run_tie" / "manifest.json"
        before = manifest.read_bytes()
        promote_run(state, artifacts, "run_tie", "reviewed perfect-score tie")
        assert manifest.read_bytes() == before
        assert json.loads((artifacts / "champion.json").read_text()) == {
            "run_id": "run_tie", "manifest_sha256": sha256_bytes(before),
        }
        _seed_candidate_run(state, artifacts, "run_other", "other@1", 0.50, 0.50, _perfect_tie_evidence())
        try:
            promote_run(state, artifacts, "run_other", "must still improve protected evaluation", 20.0)
        except PreferenceModelError as exc:
            assert "embedding promotion gates failed" in str(exc)
        else:
            raise AssertionError("perfect OOF tie must not bypass cross-encoder gates")
        with sqlite3.connect(state) as con:
            history = con.execute(
                "SELECT action,run_id,reason,forced FROM preference_champion_history ORDER BY history_id"
            ).fetchall()
        assert history == [("initial", "run_tie", "reviewed perfect-score tie", 0)]


def test_explicit_promotion_embedding_gates_and_rollback_history() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.db"
        artifacts = Path(tmp) / "artifacts"
        prepare_state(state)
        _seed_candidate_run(state, artifacts, "run_bge", "bge@1", 0.50, 0.50)
        _seed_candidate_run(state, artifacts, "run_qwen", "qwen@1", 0.52, 0.49)
        promote_run(state, artifacts, "run_bge", "initial production model")
        promote_run(
            state, artifacts, "run_qwen", "accepted embedding benchmark",
            projected_incremental_minutes=25.0,
        )
        with sqlite3.connect(state) as con:
            assert con.execute(
                "SELECT value FROM preference_state WHERE key='champion_run_id'"
            ).fetchone()[0] == "run_qwen"
            acceptance = con.execute(
                "SELECT protected_set_fingerprint,consumed_at,candidate_run_id,"
                "previous_run_id,acceptance_slice FROM preference_protected_acceptances"
            ).fetchone()
        assert acceptance[0] == protected_example_set_fingerprint({
            "example_ids": [str(index) for index in range(200)],
        })
        assert acceptance[1]
        assert acceptance[2:] == (
            "run_qwen", "run_bge", "protected_top_ranked",
        )
        rollback_run(state, artifacts, "run_bge", "recover prior champion")
        with sqlite3.connect(state) as con:
            assert con.execute(
                "SELECT value FROM preference_state WHERE key='champion_run_id'"
            ).fetchone()[0] == "run_bge"
            history = con.execute(
                "SELECT action,run_id FROM preference_champion_history ORDER BY history_id"
            ).fetchall()
        assert history == [
            ("initial", "run_bge"),
            ("promote", "run_qwen"),
            ("rollback", "run_bge"),
        ]


def test_protected_set_can_accept_only_one_cross_encoder_promotion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.db"
        artifacts = Path(tmp) / "artifacts"
        prepare_state(state)
        _seed_candidate_run(state, artifacts, "run_bge", "bge@1", 0.50, 0.50)
        _seed_candidate_run(state, artifacts, "run_qwen", "qwen@1", 0.52, 0.49)
        _seed_candidate_run(state, artifacts, "run_e5", "e5@1", 0.53, 0.50)
        promote_run(state, artifacts, "run_bge", "initial production model")
        promote_run(
            state, artifacts, "run_qwen", "first protected acceptance",
            projected_incremental_minutes=20.0,
        )
        rollback_run(state, artifacts, "run_bge", "restore control")
        try:
            promote_run(
                state, artifacts, "run_e5", "attempted protected reuse",
                projected_incremental_minutes=20.0,
            )
        except PreferenceModelError as exc:
            assert "already consumed" in str(exc)
            assert "run_qwen" in str(exc)
        else:
            raise AssertionError("expected one-shot protected-set rejection")
        with sqlite3.connect(state) as con:
            assert con.execute(
                "SELECT value FROM preference_state WHERE key='champion_run_id'"
            ).fetchone()[0] == "run_bge"
            assert con.execute(
                "SELECT COUNT(*) FROM preference_protected_acceptances"
            ).fetchone()[0] == 1
        promote_run(
            state, artifacts, "run_e5", "documented recovery override",
            projected_incremental_minutes=20.0, force=True,
        )
        with sqlite3.connect(state) as con:
            assert con.execute(
                "SELECT value FROM preference_state WHERE key='champion_run_id'"
            ).fetchone()[0] == "run_e5"
            assert con.execute(
                "SELECT COUNT(*) FROM preference_protected_acceptances"
            ).fetchone()[0] == 1


def test_embedding_switch_gate_rejects_slow_or_nonidentical_candidate() -> None:
    base = {
        "example_ids": [str(index) for index in range(200)],
        "slices": {"protected_top_ranked": {"metrics": {
            "ndcg_at_20": 0.50, "precision_at_20": 0.50,
        }}},
    }
    candidate = json.loads(json.dumps(base))
    candidate["slices"]["protected_top_ranked"]["metrics"] = {
        "ndcg_at_20": 0.52, "precision_at_20": 0.49,
    }
    failures = embedding_switch_gate(
        {"protected_evaluation": base},
        {"protected_evaluation": candidate},
        31.0,
    )
    assert failures == ["projected incremental embedding time exceeds 30 minutes"]
    candidate["example_ids"] = candidate["example_ids"][:-1]
    failures = embedding_switch_gate(
        {"protected_evaluation": base},
        {"protected_evaluation": candidate},
        20.0,
    )
    assert "identical completed protected evaluation" in failures[0]


def test_embedding_switch_acceptance_ignores_post_selection_audit_slices() -> None:
    current = {
        "example_ids": [str(index) for index in range(200)],
        "slices": {
            "protected_top_ranked": {"metrics": {
                "ndcg_at_20": 0.50, "precision_at_20": 0.50,
            }},
            "protected_uniform": {"metrics": {"ndcg_at_20": 1.0}},
            "protected_company_holdout": {"metrics": {"ndcg_at_20": 1.0}},
        },
    }
    candidate = json.loads(json.dumps(current))
    candidate["slices"]["protected_top_ranked"]["metrics"] = {
        "ndcg_at_20": 0.52, "precision_at_20": 0.49,
    }
    candidate["slices"]["protected_uniform"] = {"metrics": {"ndcg_at_20": 0.0}}
    candidate["slices"]["protected_company_holdout"] = {
        "metrics": {"ndcg_at_20": 0.0},
    }
    assert embedding_switch_gate(
        {"protected_evaluation": current},
        {"protected_evaluation": candidate},
        20.0,
    ) == []


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} preference-model tests)")


if __name__ == "__main__":
    main()
