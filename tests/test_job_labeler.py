#!/usr/bin/env python3
"""Offline tests for the local preference-labeling app."""

from __future__ import annotations

import io
import json
import re
import sqlite3
import struct
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from job_search.contracts import RecommendationProvenance

from job_search.ranking.labeler import (
    ApiError,
    HARD_BLOCKERS,
    PROGRAM_STAGES,
    PRIMARY_REASONS,
    QUALIFICATION_FITS,
    SKIP_REASONS,
    _candidate_scores,
    _filters,
    _recommendation_options,
    choose_job,
    create_shortlist_session,
    label_stats,
    make_handler,
    model_status,
    prepare_preferences,
    policy_recommendations,
    program_status,
    record_recommendation_impressions,
    recommendations,
    save_label as _save_label,
    save_recommendation_feedback,
    undo_last,
)
from job_search.collection.locations import backfill as backfill_locations


def make_database(directory: str) -> Path:
    path = Path(directory) / "jobs.db"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(path) as con:
        con.execute("""
            CREATE TABLE jobs (
                ats TEXT NOT NULL, id TEXT NOT NULL, company TEXT, title TEXT,
                department TEXT, team TEXT, employmentType TEXT, location TEXT,
                isRemote TEXT, workplaceType TEXT, publishedAt TEXT, jobUrl TEXT,
                description TEXT, matched TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                closed_at TEXT, PRIMARY KEY (ats, id)
            )
        """)
        rows = [
            ("ashby", "1", "Acme", "Product Engineer", "Engineering", "Product",
             "FullTime", "Remote - US", "True", "Remote",
             (now - timedelta(days=2)).isoformat(), "https://example.com/1",
             "Build distributed systems.", "", "", "", None),
            ("greenhouse", "2", "Beta", "Accountant", "Finance", "", "FullTime",
             "Chicago", "False", "On-site", (now - timedelta(days=10)).isoformat(),
             "https://example.com/2", "Reconcile accounts.", "", "", "", None),
            ("lever", "3", "Closed Co", "Designer", "Design", "", "Contract",
             "Remote", "True", "Remote", (now - timedelta(days=1)).isoformat(),
             "https://example.com/3", "Design products.", "", "", "", now.isoformat()),
            ("ashby", "4", "Old Co", "Old Engineer", "Engineering", "", "FullTime",
             "Boston", "False", "On-site", (now - timedelta(days=400)).isoformat(),
             "https://example.com/4", "Maintain systems.", "", "", "", None),
        ]
        con.executemany(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    prepare_preferences(path)
    return path


def seed_decisions(db: Path, count: int) -> None:
    """Insert completed program decisions without requiring matching fixture jobs."""
    rows = []
    before = 0
    for index in range(count):
        for stage in PROGRAM_STAGES:
            if index < before + stage["quota"]:
                rows.append((
                    "ashby", f"seed-{index}",
                    "interested" if index % 4 == 0 else "not_interested",
                    stage["id"], stage["sample_role"],
                    f"time-{index:04d}", f"time-{index:04d}",
                ))
                break
            before += stage["quota"]
        before = 0
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO job_preferences "
            "(ats, job_id, interest, program_stage, sample_role, labeled_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def seed_families_and_model(db: Path, model_db: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as con:
        con.executescript("""
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
            CREATE TABLE job_compensation_ranges (
                id INTEGER PRIMARY KEY, ats TEXT, job_id TEXT, range_key TEXT,
                component TEXT, value_kind TEXT, currency TEXT, period TEXT,
                min_value REAL, max_value REAL, annual_min_value REAL,
                annual_max_value REAL, location_scope TEXT, source_type TEXT,
                source_field TEXT, evidence_text TEXT, evidence_json TEXT,
                parser_rule TEXT, is_preferred INTEGER, is_corroborated INTEGER,
                first_seen TEXT, last_seen TEXT, removed_at TEXT
            );
        """)
        family_rows = [
            ("family-product", "ashby", "1", "source-1"),
            ("family-accounting", "greenhouse", "2", "source-2"),
        ]
        for family_id, ats, job_id, fingerprint in family_rows:
            con.execute(
                "INSERT INTO job_families VALUES (?,?,?,?,?,?,?,?,?,?)",
                (family_id, family_id, 1, ats, "company", "title", fingerprint,
                 ats, job_id, 1),
            )
            con.execute(
                "INSERT INTO job_family_members VALUES (?,?,?,?)",
                (ats, job_id, family_id, fingerprint),
            )
            con.execute(
                "INSERT INTO job_template_clusters VALUES (?,?,?,?)",
                (family_id, f"template-{family_id}", f"leakage-{family_id}", 1),
            )
        con.execute(
            "INSERT INTO job_compensation_ranges VALUES "
            "(1,'greenhouse','2','r','base_salary','range','USD','year',"
            "80000,90000,80000,90000,'','native','salary','','[]','rule',1,0,?,?,NULL)",
            (now, now),
        )

    with sqlite3.connect(model_db) as con:
        con.executescript("""
            CREATE TABLE preference_model_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT, model_revision TEXT,
                text_version TEXT, training_examples INTEGER,
                manifest_json TEXT, artifact_path TEXT
            );
            CREATE TABLE preference_scores (
                run_id TEXT, family_id TEXT, dense_linear_score REAL,
                dense_neighbor_score REAL, sparse_score REAL, final_score REAL,
                explanation_json TEXT, scored_at TEXT,
                PRIMARY KEY (run_id,family_id)
            );
            CREATE TABLE preference_state (key TEXT PRIMARY KEY, value TEXT);
        """)
        con.execute(
            "INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)",
            ("run-1", now, "test-model", "v1", 200, '{"ndcg_at_20":0.7}', "artifacts"),
        )
        con.execute("INSERT INTO preference_state VALUES ('champion_run_id','run-1')")
        con.executemany(
            "INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
            [
                ("run-1", "family-product", .9, .8, .7, .85,
                 '{"positive_sparse_phrases":["distributed systems"],'
                 '"similar_liked_family_ids":["family-accounting","missing-family"]}',
                 now),
                ("run-1", "family-accounting", .4, .3, .2, .35, "{}", now),
            ],
        )


def seed_evaluation_pool(db: Path, model_db: Path, count: int = 220) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS job_families (
                family_id TEXT PRIMARY KEY, family_fingerprint TEXT UNIQUE,
                normalization_version INTEGER, ats TEXT, company_normalized TEXT,
                title_normalized TEXT, description_fingerprint TEXT,
                canonical_ats TEXT, canonical_job_id TEXT, member_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS job_family_members (
                ats TEXT, job_id TEXT, family_id TEXT, source_fingerprint TEXT,
                PRIMARY KEY (ats,job_id)
            );
            CREATE TABLE IF NOT EXISTS job_template_clusters (
                family_id TEXT PRIMARY KEY, template_cluster_id TEXT,
                leakage_group_id TEXT, normalization_version INTEGER
            );
        """)
        for index in range(count):
            job_id = f"eval-{index:03d}"
            family_id = f"eval-family-{index:03d}"
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ashby", job_id, f"Eval Company {index}", f"Eval Role {index}",
                 "Engineering", "", "FullTime", "Remote", "True", "Remote", now,
                 f"https://example.com/{job_id}", f"Evaluation description {index}.",
                 "", now, now, None),
            )
            con.execute(
                "INSERT INTO job_families VALUES (?,?,?,?,?,?,?,?,?,?)",
                (family_id, family_id, 1, "ashby", f"eval company {index}",
                 f"eval role {index}", family_id, "ashby", job_id, 1),
            )
            con.execute(
                "INSERT INTO job_family_members VALUES (?,?,?,?)",
                ("ashby", job_id, family_id, family_id),
            )
            con.execute(
                "INSERT INTO job_template_clusters VALUES (?,?,?,?)",
                (family_id, f"template-{index:03d}", f"leakage-{index:03d}", 1),
            )
    with sqlite3.connect(model_db) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS preference_model_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT, model_revision TEXT,
                text_version TEXT, training_examples INTEGER,
                manifest_json TEXT, artifact_path TEXT
            );
            CREATE TABLE IF NOT EXISTS preference_scores (
                run_id TEXT, family_id TEXT, dense_linear_score REAL,
                dense_neighbor_score REAL, sparse_score REAL, final_score REAL,
                explanation_json TEXT, scored_at TEXT,
                PRIMARY KEY (run_id,family_id)
            );
            CREATE TABLE IF NOT EXISTS preference_state (key TEXT PRIMARY KEY, value TEXT);
        """)
        con.execute(
            "INSERT OR REPLACE INTO preference_model_runs VALUES (?,?,?,?,?,?,?)",
            ("eval-run", now, "test-model", "v1", 800, "{}", "artifacts"),
        )
        con.execute(
            "INSERT OR REPLACE INTO preference_state VALUES ('champion_run_id','eval-run')"
        )
        con.executemany(
            "INSERT OR REPLACE INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
            [
                ("eval-run", f"eval-family-{index:03d}", .9, .8, .7,
                 1.0 - index / (count + 1), "{}", now)
                for index in range(count)
            ],
        )


def save_label(
    db: Path,
    payload: dict,
    preference_db_path: Path | None = None,
):
    """Exercise save_label with a server-issued assignment fixture.

    Production has no tokenless fallback.  Most unit tests construct a chosen
    job directly, so this helper creates the same authoritative DB assignment
    that GET /api/job would have returned.  Security tests call _save_label
    directly to verify missing/stale tokens are rejected.
    """
    if payload.get("selection_token"):
        return _save_label(db, payload, preference_db_path)
    status = program_status(db)
    stage = status["stage"]
    ats = str(payload.get("ats", "")).strip().lower()
    job_id = str(payload.get("id", "")).strip()
    if not stage or not ats or not job_id:
        return _save_label(db, payload, preference_db_path)
    dataset_version = "preference-v1"
    family_id = f"{ats}:{job_id}"
    template_id = family_id
    leakage_id = family_id
    source_fingerprint = ""
    evaluation_position = None
    strategy = str(payload.get("selection_strategy", "test_manual"))
    probability = payload.get("selection_probability")
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        if stage["sample_role"] == "evaluation":
            queued = con.execute(
                "SELECT q.* FROM preference_evaluation_queue q "
                "WHERE q.dataset_version=? AND NOT EXISTS ("
                "SELECT 1 FROM job_preferences p JOIN preference_examples e "
                "ON p.current_example_id=e.example_id WHERE e.family_id=q.family_id) "
                "ORDER BY q.position LIMIT 1",
                (dataset_version,),
            ).fetchone()
            if not queued or queued["ats"] != ats or queued["job_id"] != job_id:
                return _save_label(db, payload, preference_db_path)
            family_id = queued["family_id"]
            template_id = queued["template_cluster_id"]
            leakage_id = queued["leakage_group_id"]
            source_fingerprint = queued["source_fingerprint"]
            evaluation_position = queued["position"]
            strategy = queued["selection_strategy"]
            probability = 1.0
        else:
            has_members = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='job_family_members'"
            ).fetchone()
            if has_members:
                family = con.execute(
                    "SELECT family_id,source_fingerprint FROM job_family_members "
                    "WHERE ats=? AND job_id=?", (ats, job_id),
                ).fetchone()
                if family:
                    family_id = family["family_id"]
                    template_id = family_id
                    leakage_id = family_id
                    source_fingerprint = family["source_fingerprint"] or ""
                    cluster = con.execute(
                        "SELECT template_cluster_id,leakage_group_id "
                        "FROM job_template_clusters WHERE family_id=?", (family_id,),
                    ).fetchone()
                    if cluster:
                        template_id = cluster["template_cluster_id"] or family_id
                        leakage_id = cluster["leakage_group_id"] or family_id
        token = f"test-{datetime.now(timezone.utc).timestamp()}-{ats}-{job_id}"
        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            "UPDATE preference_selection_tokens SET consumed_at=? "
            "WHERE dataset_version=? AND consumed_at IS NULL", (now, dataset_version),
        )
        con.execute(
            "INSERT INTO preference_selection_tokens "
            "(token,dataset_version,program_stage,sample_role,ats,job_id,family_id,"
            "template_cluster_id,leakage_group_id,source_fingerprint,selection_strategy,"
            "selection_probability,evaluation_position,issued_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                token, dataset_version, stage["id"], stage["sample_role"], ats,
                job_id, family_id, template_id, leakage_id, source_fingerprint,
                strategy, probability, evaluation_position, now,
            ),
        )
    authorized = dict(payload)
    authorized["selection_token"] = token
    return _save_label(db, authorized, preference_db_path)


def test_choose_and_filter() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        job = choose_job(db, _filters({"days": ["7"]}))
        assert job and job["id"] == "1", job
        assert job["description"] == "Build distributed systems."
        remote = choose_job(db, _filters({"days": ["30"], "remote": ["true"]}))
        assert remote and remote["id"] == "1", remote
        searched = choose_job(db, _filters({"days": ["30"], "search": ["account"]}))
        assert searched and searched["id"] == "2", searched


def test_label_excludes_job_and_records_optional_signals() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        saved = save_label(db, {
            "ats": "ashby", "id": "1", "interest": "interested",
            "qualification_fit": "stretch", "primary_reason": "growth_scope",
            "hard_blockers": ["location", "travel"], "note": "Good scope",
        })
        assert saved["ok"]
        assert choose_job(db, _filters({"days": ["7"]})) is None
        with sqlite3.connect(db) as con:
            row = con.execute(
                "SELECT interest, qualification_fit, primary_reason, "
                "hard_blockers, program_stage, sample_role, note FROM job_preferences"
            ).fetchone()
        assert row == (
            "interested", "stretch", "growth_scope", '["location", "travel"]',
            1, "training", "Good scope"
        ), row


def test_upsert_stats_and_undo() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        save_label(db, {"ats": "ashby", "id": "1", "interest": "maybe"})
        save_label(db, {"ats": "greenhouse", "id": "2", "interest": "not_interested"})
        stats = label_stats(db, _filters({"days": ["30"]}))
        assert stats["labeled"] == 2 and stats["maybe"] == 1, stats
        assert stats["not_interested"] == 1 and stats["unlabeled"] == 0, stats
        assert stats["program"]["decisive"] == 1
        assert stats["program"]["stage"]["name"] == "Calibration"
        save_label(db, {
            "ats": "ashby", "id": "1", "interest": "maybe",
            "qualification_fit": "plausible", "primary_reason": "domain",
        })
        undone = undo_last(db)
        assert undone and undone["ats"] == "ashby" and undone["job_id"] == "1", undone
        assert label_stats(db, _filters({"days": ["30"]}))["labeled"] == 1


def test_label_events_audit_create_update_and_undo() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        save_label(db, {"ats": "ashby", "id": "1", "interest": "maybe"})
        save_label(db, {
            "ats": "ashby", "id": "1", "interest": "interested",
            "note": "corrected",
        })
        undo_last(db)
        with sqlite3.connect(db) as con:
            events = con.execute(
                "SELECT action,interest,example_id,payload_json "
                "FROM preference_label_events ORDER BY event_id"
            ).fetchall()
            assert [row[0] for row in events] == ["create", "update", "undo"]
            assert [row[1] for row in events] == ["maybe", "interested", "interested"]
            assert events[0][2] != events[1][2] == events[2][2]
            assert json.loads(events[2][3])["note"] == "corrected"
            assert con.execute("SELECT COUNT(*) FROM preference_examples").fetchone()[0] == 2
            for statement in (
                "UPDATE preference_label_events SET interest='maybe' WHERE event_id=1",
                "DELETE FROM preference_label_events WHERE event_id=1",
            ):
                try:
                    con.execute(statement)
                    raise AssertionError("label audit event was mutable")
                except sqlite3.IntegrityError:
                    pass


def test_guided_stages_count_only_decisions_and_protect_evaluation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        initial = program_status(db)
        assert initial["stage"]["name"] == "Calibration"
        assert initial["stage"]["quota"] == 50

        save_label(db, {"ats": "ashby", "id": "1", "interest": "maybe"})
        save_label(db, {
            "ats": "greenhouse", "id": "2", "interest": "skipped",
            "skip_reason": "insufficient_information",
        })
        after_nondescisions = program_status(db)
        assert after_nondescisions["decisive"] == 0
        assert after_nondescisions["counts"]["maybe"] == 1
        assert after_nondescisions["counts"]["skipped"] == 1

    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        seed_decisions(db, 49)
        result = save_label(db, {
            "ats": "ashby", "id": "1", "interest": "interested",
        })
        assert result["stage_transition"]
        assert result["program"]["stage"]["name"] == "Foundation"
        assert result["program"]["stage"]["completed"] == 0

    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        seed_decisions(db, 800)
        model_db = Path(directory) / "model.db"
        seed_evaluation_pool(db, model_db)
        status = program_status(db)
        assert status["stage"]["name"] == "Evaluation"
        selected = choose_job(db, _filters({"days": ["30"]}), model_db)
        result = _save_label(db, {
            "ats": selected["ats"], "id": selected["id"],
            "selection_token": selected["selection_token"],
            "interest": "not_interested",
        }, model_db)
        with sqlite3.connect(db) as con:
            role = con.execute(
                "SELECT program_stage, sample_role FROM job_preferences "
                "WHERE ats=? AND job_id=?",
                (selected["ats"], selected["id"]),
            ).fetchone()
        assert role == (5, "evaluation")
        assert result["program"]["decisive"] == 801

    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        seed_decisions(db, 999)
        model_db = Path(directory) / "model.db"
        seed_evaluation_pool(db, model_db)
        selected = choose_job(db, _filters({"days": ["30"]}), model_db)
        result = _save_label(db, {
            "ats": selected["ats"], "id": selected["id"],
            "selection_token": selected["selection_token"],
            "interest": "interested",
        }, model_db)
        assert result["stage_transition"] and result["program"]["complete"]
        assert result["program"]["overall_remaining"] == 0
        assert choose_job(db, _filters({"days": ["30"]})) is None


def test_selection_tokens_reject_two_tabs_stage_changes_and_direct_eval_posts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "model.db"
        seed_decisions(db, 799)
        seed_evaluation_pool(db, model_db)

        stale_training = choose_job(db, _filters({"days": ["30"]}), model_db)
        current_training = choose_job(db, _filters({"days": ["30"]}), model_db)
        assert stale_training["selection_program_stage"] == 4
        _save_label(db, {
            "ats": current_training["ats"], "id": current_training["id"],
            "selection_token": current_training["selection_token"],
            "interest": "interested",
        }, model_db)
        assert program_status(db)["stage"]["name"] == "Evaluation"
        try:
            _save_label(db, {
                "ats": stale_training["ats"], "id": stale_training["id"],
                "selection_token": stale_training["selection_token"],
                "interest": "interested",
            }, model_db)
            raise AssertionError("accepted a superseded training token")
        except ApiError as exc:
            assert exc.status == 409

        evaluation_a = choose_job(db, _filters({"days": ["30"]}), model_db)
        evaluation_b = choose_job(db, _filters({"days": ["30"]}), model_db)
        assert evaluation_a["id"] == evaluation_b["id"]
        assert evaluation_a["selection_token"] != evaluation_b["selection_token"]
        try:
            _save_label(db, {
                "ats": evaluation_a["ats"], "id": evaluation_a["id"],
                "selection_token": evaluation_a["selection_token"],
                "interest": "not_interested",
            }, model_db)
            raise AssertionError("accepted a superseded evaluation token")
        except ApiError as exc:
            assert exc.status == 409
        try:
            _save_label(db, {
                "ats": evaluation_b["ats"], "id": evaluation_b["id"],
                "interest": "not_interested",
            }, model_db)
            raise AssertionError("accepted a direct protected-evaluation POST")
        except ApiError as exc:
            assert exc.status == 409
        accepted = _save_label(db, {
            "ats": evaluation_b["ats"], "id": evaluation_b["id"],
            "selection_token": evaluation_b["selection_token"],
            "interest": "not_interested",
            # Client attempts cannot override the frozen assignment metadata.
            "selection_strategy": "uniform_audit",
            "selection_probability": 0.01,
        }, model_db)
        assert accepted["ok"]
        with sqlite3.connect(db) as con:
            example = con.execute(
                "SELECT selection_strategy,selection_probability,sample_role "
                "FROM preference_examples WHERE ats=? AND job_id=?",
                (evaluation_b["ats"], evaluation_b["id"]),
            ).fetchone()
        assert example == ("protected_top_ranked", 1.0, "evaluation")


def test_protected_evaluation_serves_and_labels_the_frozen_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "model.db"
        seed_decisions(db, 800)
        seed_evaluation_pool(db, model_db)
        original = choose_job(db, _filters({"days": ["30"]}), model_db)
        frozen = {
            key: original[key]
            for key in (
                "ats", "id", "title", "description", "jobUrl", "family_id",
                "template_cluster_id", "leakage_group_id", "source_fingerprint",
            )
        }
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE jobs SET title='Mutated title',description='Mutated body',"
                "jobUrl='https://mutated.invalid' WHERE ats=? AND id=?",
                (original["ats"], original["id"]),
            )
            con.execute(
                "UPDATE job_family_members SET family_id='eval-family-219',"
                "source_fingerprint='mutated-source' WHERE ats=? AND job_id=?",
                (original["ats"], original["id"]),
            )
            con.execute(
                "UPDATE job_template_clusters SET template_cluster_id='mutated-template',"
                "leakage_group_id='mutated-leakage' WHERE family_id=?",
                (original["family_id"],),
            )

        served = choose_job(db, _filters({"days": ["30"]}), model_db)
        for key, value in frozen.items():
            assert served[key] == value, (key, served[key], value)
        _save_label(db, {
            "ats": served["ats"], "id": served["id"],
            "selection_token": served["selection_token"],
            "interest": "interested",
        }, model_db)
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            example = con.execute(
                "SELECT * FROM preference_examples WHERE ats=? AND job_id=?",
                (served["ats"], served["id"]),
            ).fetchone()
        metadata = json.loads(example["metadata_json"])
        assert example["title_snapshot"] == frozen["title"]
        assert example["description_snapshot"] == frozen["description"]
        assert example["family_id"] == frozen["family_id"]
        assert example["template_cluster_id"] == frozen["template_cluster_id"]
        assert example["leakage_group_id"] == frozen["leakage_group_id"]
        assert example["source_fingerprint"] == frozen["source_fingerprint"]
        assert metadata["jobUrl"] == frozen["jobUrl"]


def test_invalid_values_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        for payload in (
            {"ats": "ashby", "id": "1", "interest": "undecided"},
            {"ats": "ashby", "id": "1", "interest": "interested", "qualification_fit": "vp"},
            {"ats": "ashby", "id": "1", "interest": "interested", "primary_reason": "free_lunch"},
            {"ats": "ashby", "id": "1", "interest": "interested", "hard_blockers": ["boring"]},
            {"ats": "ashby", "id": "1", "interest": "skipped", "skip_reason": "tired"},
        ):
            try:
                save_label(db, payload)
                raise AssertionError(f"accepted invalid payload: {payload}")
            except ApiError:
                pass
        try:
            _filters({"days": ["13"]})
            raise AssertionError("accepted invalid days filter")
        except ApiError:
            pass


def test_migrates_the_original_preference_schema() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "jobs.db"
        make_database(directory)
        with sqlite3.connect(db) as con:
            con.execute("DROP TABLE job_preferences")
            con.executescript("""
                CREATE TABLE job_preferences (
                    ats TEXT NOT NULL, job_id TEXT NOT NULL,
                    interest TEXT NOT NULL CHECK (
                        interest IN ('interested', 'not_interested')
                    ),
                    seniority_fit TEXT NOT NULL DEFAULT '',
                    signal TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    labeled_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (ats, job_id)
                );
                CREATE INDEX job_preferences_interest
                    ON job_preferences(interest, updated_at);
            """)
            con.execute(
                "INSERT INTO job_preferences VALUES "
                "('ashby','1','interested','right_level','skills','legacy','then','now')"
            )

        prepare_preferences(db)
        with sqlite3.connect(db) as con:
            cols = {row[1] for row in con.execute(
                "PRAGMA table_info(job_preferences)"
            )}
            migrated = con.execute(
                "SELECT interest, qualification_fit, primary_reason, hard_blockers, "
                "program_stage, sample_role, note, labeled_at, updated_at "
                "FROM job_preferences"
            ).fetchone()
        assert {
            "qualification_fit", "primary_reason", "hard_blockers", "skip_reason",
            "program_stage", "sample_role",
        } <= cols
        assert "seniority_fit" not in cols and "signal" not in cols
        assert migrated == (
            "interested", "strong", "skills", "[]", 1, "training",
            "legacy", "then", "now"
        )

        # The rebuilt CHECK constraint must accept uncertainty and non-preference skips.
        save_label(db, {"ats": "greenhouse", "id": "2", "interest": "maybe"})
        save_label(db, {
            "ats": "lever", "id": "3", "interest": "skipped",
            "skip_reason": "not_a_job",
        })


def test_examples_are_immutable_snapshots_and_splits_are_protected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        first = choose_job(db, _filters({"days": ["7"]}))
        save_label(db, {
            "ats": "ashby", "id": "1", "interest": "interested",
            "selection_strategy": first["selection_strategy"],
            "selection_probability": first["selection_probability"],
        })
        with sqlite3.connect(db) as con:
            original = con.execute(
                "SELECT example_id,title_snapshot,description_snapshot,metadata_json,"
                "family_id,template_cluster_id,leakage_group_id,source_fingerprint,"
                "selection_strategy,selection_probability,dataset_version "
                "FROM preference_examples"
            ).fetchone()
            con.execute(
                "UPDATE jobs SET title='Changed title',description='Changed body' "
                "WHERE ats='ashby' AND id='1'"
            )
        save_label(db, {
            "ats": "ashby", "id": "1", "interest": "not_interested",
            "selection_strategy": "manual", "selection_probability": 1.0,
        })
        with sqlite3.connect(db) as con:
            snapshots = con.execute(
                "SELECT title_snapshot,description_snapshot FROM preference_examples "
                "ORDER BY example_id"
            ).fetchall()
            training = con.execute(
                "SELECT interest,title_snapshot FROM preference_training_examples"
            ).fetchall()
            evaluation = con.execute(
                "SELECT COUNT(*) FROM preference_evaluation_examples"
            ).fetchone()[0]
            try:
                con.execute(
                    "UPDATE preference_examples SET title_snapshot='mutated' "
                    "WHERE example_id=?", (original[0],),
                )
                raise AssertionError("immutable example accepted an update")
            except sqlite3.IntegrityError:
                pass
        assert snapshots == [
            ("Product Engineer", "Build distributed systems."),
            ("Changed title", "Changed body"),
        ]
        assert training == [("not_interested", "Changed title")]
        assert evaluation == 0
        assert original[4:7] == ("ashby:1", "ashby:1", "ashby:1")
        assert len(original[7]) == 64
        assert original[8] in {"diversity", "diversity_fallback", "uniform"}
        assert original[9] == .70 and original[10] == "preference-v1"

    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        seed_decisions(db, 800)
        model_db = Path(directory) / "jobs-preference.db"
        seed_evaluation_pool(db, model_db)
        job = choose_job(db, _filters({"days": ["7"]}), model_db)
        assert job["selection_strategy"] == "protected_top_ranked"
        assert "final_score" not in job and "score_components" not in job
        save_label(db, {
            "ats": job["ats"], "id": job["id"], "interest": "interested",
            "selection_strategy": job["selection_strategy"],
            "selection_probability": job["selection_probability"],
        })
        with sqlite3.connect(db) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM preference_training_examples"
            ).fetchone()[0] == 0
            assert con.execute(
                "SELECT COUNT(*) FROM preference_evaluation_examples"
            ).fetchone()[0] == 1


def test_family_aware_selection_gracefully_falls_back() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        # No family tables is a supported pre-dedupe state.
        assert choose_job(db, _filters({"days": ["7"]}))["family_id"] == "ashby:1"

    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        with sqlite3.connect(db) as con:
            con.executescript("""
                CREATE TABLE job_family_members (
                    ats TEXT, job_id TEXT, family_id TEXT, source_fingerprint TEXT,
                    PRIMARY KEY (ats,job_id)
                );
                CREATE TABLE job_template_clusters (
                    family_id TEXT PRIMARY KEY, template_cluster_id TEXT,
                    leakage_group_id TEXT, normalization_version INTEGER
                );
            """)
            con.executemany(
                "INSERT INTO job_family_members VALUES (?,?,?,?)",
                [("ashby", "1", "shared", "one"),
                 ("greenhouse", "2", "shared", "two")],
            )
            con.execute(
                "INSERT INTO job_template_clusters VALUES ('shared','template','leakage',1)"
            )
        save_label(db, {"ats": "ashby", "id": "1", "interest": "interested"})
        other = choose_job(db, _filters({"days": ["30"], "search": ["account"]}))
        assert other is None, other
        with sqlite3.connect(db) as con:
            snapshot = con.execute(
                "SELECT family_id,template_cluster_id,leakage_group_id "
                "FROM preference_examples"
            ).fetchone()
        assert snapshot == ("shared", "template", "leakage")


def test_protected_evaluation_never_crosses_a_training_leakage_group() -> None:
    for shared_kind in ("leakage", "template"):
        with tempfile.TemporaryDirectory() as directory:
            db = make_database(directory)
            with sqlite3.connect(db) as con:
                con.executescript("""
                    CREATE TABLE job_family_members (
                        ats TEXT, job_id TEXT, family_id TEXT, source_fingerprint TEXT,
                        PRIMARY KEY (ats,job_id)
                    );
                    CREATE TABLE job_template_clusters (
                        family_id TEXT PRIMARY KEY, template_cluster_id TEXT,
                        leakage_group_id TEXT, normalization_version INTEGER
                    );
                """)
                con.executemany(
                    "INSERT INTO job_family_members VALUES (?,?,?,?)",
                    [("ashby", "1", "family-a", "one"),
                     ("greenhouse", "2", "family-b", "two")],
                )
                clusters = (
                    [("family-a", "template-a", "shared"),
                     ("family-b", "template-b", "shared")]
                    if shared_kind == "leakage" else
                    [("family-a", "shared", "description-a"),
                     ("family-b", "shared", "description-b")]
                )
                con.executemany(
                    "INSERT INTO job_template_clusters VALUES (?,?,?,1)", clusters,
                )
            save_label(db, {"ats": "ashby", "id": "1", "interest": "interested"})
            seed_decisions(db, 799)
            model_db = Path(directory) / "model.db"
            seed_evaluation_pool(db, model_db)
            now = datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(model_db) as con:
                con.execute(
                    "INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
                    ("eval-run", "family-b", .99, .99, .99, .99, "{}", now),
                )
            choose_job(db, _filters({"days": ["30"]}), model_db)
            with sqlite3.connect(db) as con:
                queued = con.execute(
                    "SELECT COUNT(*) FROM preference_evaluation_queue"
                ).fetchone()[0]
                leaked = con.execute(
                    "SELECT COUNT(*) FROM preference_evaluation_queue "
                    "WHERE family_id='family-b'"
                ).fetchone()[0]
            assert queued == 200 and leaked == 0


def test_protected_evaluation_uses_three_hidden_slices_and_requires_champion() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        seed_decisions(db, 800)
        model_db = Path(directory) / "jobs-preference.db"
        seed_evaluation_pool(db, model_db)
        first = choose_job(db, _filters({"days": ["30"]}), model_db)
        assert first["selection_strategy"] == "protected_top_ranked"
        assert "final_score" not in first and "score_components" not in first
        with sqlite3.connect(db) as con:
            slices = dict(con.execute(
                "SELECT selection_strategy,COUNT(*) FROM preference_evaluation_queue "
                "GROUP BY selection_strategy"
            ))
            queue = con.execute(
                "SELECT position,ats,job_id FROM preference_evaluation_queue "
                "ORDER BY position"
            ).fetchall()
        assert slices == {
            "protected_top_ranked": 100,
            "protected_uniform": 50,
            "protected_company_holdout": 50,
        }

        # Changing the champion after materialization cannot alter the frozen queue.
        with sqlite3.connect(model_db) as con:
            con.execute(
                "INSERT INTO preference_model_runs VALUES (?,?,?,?,?,?,?)",
                ("new-run", "now", "new", "v2", 800, "{}", "artifact"),
            )
            con.execute(
                "UPDATE preference_state SET value='new-run' WHERE key='champion_run_id'"
            )
        again = choose_job(db, _filters({"days": ["30"]}), model_db)
        assert again["family_id"] == first["family_id"]
        assert again["model_run_id"] == "eval-run"

        for _, ats, job_id in queue[:100]:
            save_label(db, {"ats": ats, "id": job_id, "interest": "interested"})
        uniform = choose_job(db, _filters({"days": ["30"]}), model_db)
        assert uniform["selection_strategy"] == "protected_uniform"
        for _, ats, job_id in queue[100:150]:
            save_label(db, {"ats": ats, "id": job_id, "interest": "interested"})
        holdout = choose_job(db, _filters({"days": ["30"]}), model_db)
        assert holdout["selection_strategy"] == "protected_company_holdout"

    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        seed_decisions(db, 800)
        seeded_model = Path(directory) / "seeded.db"
        seed_families_and_model(db, seeded_model)
        try:
            choose_job(db, _filters({"days": ["30"]}), Path(directory) / "missing.db")
            raise AssertionError("evaluation continued without a champion model")
        except ApiError as exc:
            assert exc.status == 409

    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        seed_decisions(db, 800)
        model_db = Path(directory) / "too-small.db"
        seed_evaluation_pool(db, model_db, count=150)
        try:
            choose_job(db, _filters({"days": ["30"]}), model_db)
            raise AssertionError("froze a partial protected queue")
        except ApiError as exc:
            assert exc.status == 409 and "cannot freeze" in str(exc)
        with sqlite3.connect(db) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM preference_evaluation_queue"
            ).fetchone()[0] == 0


def test_recommendations_use_sidecar_collapse_and_salary_unknown_is_allowed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db) as con:
            # A bonus alone is not primary compensation and therefore cannot prove
            # that this otherwise unknown role misses the user's salary floor.
            con.execute(
                "INSERT INTO job_compensation_ranges VALUES "
                "(2,'ashby','1','bonus','bonus','range','USD','year',"
                "10000,20000,10000,20000,'','native','bonus','','[]','rule',1,0,?,?,NULL)",
                (now, now),
            )
        status = model_status(model_db)
        assert status["ready"] and status["score_count"] == 2
        options = _recommendation_options({"limit": ["20"], "salary_floor": ["100000"]})
        result = recommendations(db, model_db, options)
        jobs = result["recommendations"]
        assert len(jobs) == 1, jobs
        assert jobs[0]["family_id"] == "family-product"
        assert jobs[0]["salary"]["status"] == "unknown"
        assert jobs[0]["salary"]["ranges"] == []
        assert jobs[0]["variant_count"] == 1
        assert jobs[0]["score_components"]["dense_linear"] == .9
        explanation = jobs[0]["explanation"]
        assert explanation["positive_sparse_phrases"] == ["distributed systems"]
        assert explanation["similar_liked_family_ids"] == [{
            "family_id": "family-accounting",
            "title": "Accountant",
            "company": "Beta",
        }]
        assert "missing-family" not in json.dumps(explanation)

        limited = recommendations(
            db, model_db, _recommendation_options({"limit": ["1"]})
        )
        assert len(limited["recommendations"]) == 1
        assert limited["options"]["limit"] == 1


def test_default_shortlist_has_eighteen_ranked_and_two_exploration_jobs() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db) as con:
            for index in range(5, 25):
                job_id = str(index)
                family_id = f"family-{index}"
                con.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("ashby", job_id, f"Company {index}", f"Role {index}",
                     "Engineering", "", "FullTime", "Remote", "True", "Remote",
                     now, f"https://example.com/{index}", f"Build system {index}.",
                     "", now, now, None),
                )
                con.execute(
                    "INSERT INTO job_families VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (family_id, family_id, 1, "ashby", f"company {index}",
                     f"role {index}", family_id, "ashby", job_id, 1),
                )
                con.execute(
                    "INSERT INTO job_family_members VALUES (?,?,?,?)",
                    ("ashby", job_id, family_id, family_id),
                )
                con.execute(
                    "INSERT INTO job_template_clusters VALUES (?,?,?,?)",
                    (family_id, family_id, family_id, 1),
                )
        with sqlite3.connect(model_db) as con:
            for index in range(5, 25):
                value = 1.0 - index / 100
                con.execute(
                    "INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
                    ("run-1", f"family-{index}", value, value, value,
                     value, "{}", now),
                )
        result = recommendations(db, model_db, _recommendation_options({}))
        segments = [job["segment"] for job in result["recommendations"]]
        assert len(segments) == 20
        assert segments.count("exploit") == 18, segments
        assert segments.count("explore") == 2, segments


def test_dense_title_mmr_diversifies_exploit_results() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db) as con:
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("lever", "5", "Gamma", "Product Engineer II", "Engineering", "",
                 "FullTime", "Remote", "True", "Remote", now,
                 "https://example.com/5", "Build distributed products.", "",
                 now, now, None),
            )
            con.execute(
                "INSERT INTO job_families VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("family-near-product", "near", 1, "lever", "gamma",
                 "product engineer ii", "near", "lever", "5", 1),
            )
            con.execute(
                "INSERT INTO job_family_members VALUES (?,?,?,?)",
                ("lever", "5", "family-near-product", "near"),
            )
            con.execute(
                "INSERT INTO job_template_clusters VALUES (?,?,?,?)",
                ("family-near-product", "near", "near", 1),
            )
        with sqlite3.connect(model_db) as con:
            con.executescript("""
                CREATE TABLE preference_embedding_refs (
                    subject_type TEXT, subject_id TEXT, model_revision TEXT,
                    text_version TEXT, title_fingerprint TEXT
                );
                CREATE TABLE preference_embedding_cache (
                    model_revision TEXT, text_version TEXT,
                    text_fingerprint TEXT, vector_blob BLOB, dimensions INTEGER
                );
            """)
            con.execute(
                "UPDATE preference_scores SET dense_linear_score=.95,final_score=.95 "
                "WHERE family_id='family-product'"
            )
            con.execute(
                "UPDATE preference_scores SET dense_linear_score=.85,final_score=.85 "
                "WHERE family_id='family-accounting'"
            )
            con.execute(
                "INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
                ("run-1", "family-near-product", .94, .9, .9, .94, "{}", now),
            )
            con.executemany(
                "INSERT INTO preference_embedding_refs VALUES ('family',?,'test-model','v1',?)",
                [("family-product", "fp-product"),
                 ("family-near-product", "fp-near"),
                 ("family-accounting", "fp-accounting")],
            )
            con.executemany(
                "INSERT INTO preference_embedding_cache VALUES "
                "('test-model','v1',?,?,2)",
                [("fp-product", struct.pack("<2f", 1.0, 0.0)),
                 ("fp-near", struct.pack("<2f", 1.0, 0.0)),
                 ("fp-accounting", struct.pack("<2f", 0.0, 1.0))],
            )
        result = recommendations(db, model_db, _recommendation_options({"limit": ["2"]}))
        families = [row["family_id"] for row in result["recommendations"]]
        assert families == ["family-product", "family-accounting"], families


def test_recommendations_examine_a_bounded_score_pool() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(model_db) as con:
            con.executemany(
                "INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("run-1", f"missing-{index}", .99, .99, .99, .99, "{}", now)
                    for index in range(15000)
                ],
            )
        result = recommendations(db, model_db, _recommendation_options({}))
        assert result["model"]["score_count"] == 15002
        assert result["candidate_pool_examined"] <= 11000
        assert result["candidate_pool_examined"] < result["model"]["score_count"]
        assert result["candidate_pool_truncated"]


def test_label_sampling_reads_only_candidate_scores_from_large_sidecar() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(model_db) as con:
            con.executemany(
                "INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("run-1", f"unrelated-{index}", .9, .9, .9, .9,
                     '{"large":"unused"}', now)
                    for index in range(15000)
                ],
            )
        scores = _candidate_scores(
            model_db, ["family-product", "candidate-without-a-score"]
        )
        assert set(scores) == {"family-product"}
        assert set(scores["family-product"]) == {
            "family_id", "final_score", "dense_linear_score",
            "dense_neighbor_score", "sparse_score",
        }


def test_first_stage_diversity_uses_active_embeddings_without_champion() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        save_label(db, {"ats": "ashby", "id": "1", "interest": "interested"})
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db) as con:
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("lever", "5", "Gamma", "Product Engineer II", "Engineering", "",
                 "FullTime", "Remote", "True", "Remote", now,
                 "https://example.com/5", "Build distributed products.", "",
                 now, now, None),
            )
            con.execute(
                "INSERT INTO job_families VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("family-near", "near", 1, "lever", "gamma",
                 "product engineer ii", "near", "lever", "5", 1),
            )
            con.execute(
                "INSERT INTO job_family_members VALUES (?,?,?,?)",
                ("lever", "5", "family-near", "near"),
            )
            con.execute(
                "INSERT INTO job_template_clusters VALUES (?,?,?,?)",
                ("family-near", "near", "near", 1),
            )
        with sqlite3.connect(model_db) as con:
            con.execute("DELETE FROM preference_state WHERE key='champion_run_id'")
            con.executemany(
                "INSERT INTO preference_state VALUES (?,?)",
                [("embedding_model_revision", "embed-model"), ("text_version", "v1")],
            )
            con.executescript("""
                CREATE TABLE preference_embedding_refs (
                    subject_type TEXT, subject_id TEXT, model_revision TEXT,
                    text_version TEXT, title_fingerprint TEXT
                );
                CREATE TABLE preference_embedding_cache (
                    model_revision TEXT, text_version TEXT, text_fingerprint TEXT,
                    vector_blob BLOB, dimensions INTEGER
                );
            """)
            con.executemany(
                "INSERT INTO preference_embedding_refs VALUES ('family',?,'embed-model','v1',?)",
                [("family-product", "product"), ("family-near", "near"),
                 ("family-accounting", "accounting")],
            )
            con.executemany(
                "INSERT INTO preference_embedding_cache VALUES ('embed-model','v1',?,?,2)",
                [("product", struct.pack("<2f", 1.0, 0.0)),
                 ("near", struct.pack("<2f", 1.0, 0.0)),
                 ("accounting", struct.pack("<2f", 0.0, 1.0))],
            )
        job = choose_job(db, _filters({"days": ["30"]}), model_db)
        assert job["selection_strategy"] == "diversity"
        assert job["family_id"] == "family-accounting", job
        assert "final_score" not in job


def test_recommendations_page_past_stale_high_scores() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db) as con:
            con.execute("UPDATE jobs SET closed_at=? WHERE id IN ('1','2')", (now,))
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("lever", "5", "Late Co", "Late Ranked Role", "Engineering", "",
                 "FullTime", "Remote", "True", "Remote", now,
                 "https://example.com/5", "A valid lower-ranked role.", "", now, now, None),
            )
            con.execute(
                "INSERT INTO job_families VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("family-late", "late", 1, "lever", "late co", "late ranked role",
                 "late", "lever", "5", 1),
            )
            con.execute(
                "INSERT INTO job_family_members VALUES (?,?,?,?)",
                ("lever", "5", "family-late", "late"),
            )
            con.execute(
                "INSERT INTO job_template_clusters VALUES (?,?,?,?)",
                ("family-late", "late", "late", 1),
            )
        with sqlite3.connect(model_db) as con:
            con.executemany(
                "INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("run-1", f"stale-{index}", .99, .99, .99, .99, "{}", now)
                    for index in range(700)
                ],
            )
            con.execute(
                "INSERT INTO preference_scores VALUES (?,?,?,?,?,?,?,?)",
                ("run-1", "family-late", .01, .01, .01, .01, "{}", now),
            )
        result = recommendations(db, model_db, _recommendation_options({"limit": ["1"]}))
        assert result["candidate_pool_examined"] > 500
        assert [row["family_id"] for row in result["recommendations"]] == ["family-late"]
        assert result["recommendations"][0]["first_seen"] == now


def test_feedback_is_separate_from_preference_labels() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        for action in (
            "applied", "saved", "dismissed_preference", "blocked", "duplicate",
        ):
            result = save_recommendation_feedback(db, {
                "ats": "ashby", "id": "1", "action": action,
                "rank": 1, "model_run_id": "run-1",
            })
            assert result["ok"] and not result["preference_label_changed"]
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM recommendation_feedback").fetchone()[0] == 5
            assert con.execute("SELECT COUNT(*) FROM job_preferences").fetchone()[0] == 0
        try:
            save_recommendation_feedback(db, {
                "ats": "ashby", "id": "1", "action": "viewed",
            })
            raise AssertionError("accepted unsupported feedback action")
        except ApiError:
            pass


def test_shortlist_entropy_prefix_always_crosses_application_boundary():
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        prepare_preferences(db)
        for prefix in ("-", "_"):
            with patch("job_search.ranking.labeler.secrets.token_urlsafe", return_value=prefix + "a" * 23):
                result = record_recommendation_impressions(db, {"recommendations": [], "options": {}}, "champion")
            RecommendationProvenance(session_id=result["session_id"]).validate()
            assert result["session_id"].endswith(prefix + "a" * 23)


def test_shortlist_sessions_and_feedback_are_idempotent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        proxy_db = Path(directory) / "jobs-proxy.db"
        seed_families_and_model(db, model_db)
        options = _recommendation_options({"limit": ["1"]})
        first = create_shortlist_session(
            db, model_db, proxy_db, options,
            idempotency_key="shortlist:one", actor="dashboard",
        )
        replay = create_shortlist_session(
            db, model_db, proxy_db, options,
            idempotency_key="shortlist:one", actor="dashboard",
        )
        assert replay == first
        row = first["recommendations"][0]
        payload = {
            "ats": row["ats"], "id": row["id"], "action": "applied",
            "rank": 999, "model_run_id": "untrusted",
            "impression_id": row["impression_id"],
        }
        created = save_recommendation_feedback(
            db, payload, source_event_id="application_event:event-1",
        )
        replayed = save_recommendation_feedback(
            db, payload, source_event_id="application_event:event-1",
        )
        assert created["created"] and not replayed["created"]
        assert created["feedback_id"] == replayed["feedback_id"]
        with sqlite3.connect(db) as con:
            stored = con.execute(
                "SELECT recommendation_rank,model_run_id,source_event_id "
                "FROM recommendation_feedback"
            ).fetchone()
        assert stored == (1, row["model_run_id"], "application_event:event-1")


def test_existing_application_keys_suppress_current_family() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        options = _recommendation_options({"limit": ["2"]})
        result = recommendations(
            db, model_db, options,
            excluded_job_keys={("ashby", "1")},
        )
        assert "family-product" not in {
            row["family_id"] for row in result["recommendations"]
        }


def test_proxy_policy_compare_and_passive_feedback_snapshots() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        proxy_db = Path(directory) / "jobs-proxy.db"
        seed_families_and_model(db, model_db)
        backfill_locations(db)
        with sqlite3.connect(proxy_db) as con:
            con.execute("CREATE TABLE proxy_runs (run_id TEXT,created_at TEXT)")
            con.execute(
                "CREATE TABLE proxy_students (run_id TEXT,policy_id TEXT,model_run_id TEXT,"
                "trained_at TEXT)"
            )
            con.execute("INSERT INTO proxy_runs VALUES ('proxy-1','2026-01-01')")
            con.executemany(
                "INSERT INTO proxy_students VALUES ('proxy-1',?,'run-1','2026-01-02')",
                [("selective",), ("broad",)],
            )
        options = _recommendation_options({"limit": ["2"], "policy": ["compare"]})
        result = policy_recommendations(db, model_db, proxy_db, options)
        assert result["model"]["ready"]
        assert {row["policy_id"] for row in result["recommendations"]} == {
            "selective", "broad",
        }
        result = record_recommendation_impressions(db, result, "compare")
        first = result["recommendations"][0]
        save_recommendation_feedback(db, {
            "ats": first["ats"], "id": first["id"], "action": "applied",
            "rank": first["rank"], "model_run_id": first["model_run_id"],
            "policy_id": first["policy_id"], "session_id": first["session_id"],
            "impression_id": first["impression_id"],
        })
        with sqlite3.connect(db) as con:
            stored = con.execute(
                "SELECT implicit_weight,title_snapshot,description_snapshot,session_id "
                "FROM recommendation_feedback"
            ).fetchone()
        assert stored[0] == 2.0 and stored[1] and stored[2]
        assert stored[3] == result["session_id"]


def test_derived_sparse_policy_requires_a_complete_matching_refresh() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        proxy_db = Path(directory) / "jobs-proxy.db"
        seed_families_and_model(db, model_db)
        backfill_locations(db)
        with sqlite3.connect(proxy_db) as con:
            con.executescript("""
                CREATE TABLE proxy_runs (run_id TEXT,created_at TEXT);
                CREATE TABLE proxy_students (run_id TEXT,policy_id TEXT,model_run_id TEXT,trained_at TEXT);
                INSERT INTO proxy_runs VALUES ('proxy-1','2026-01-01');
                INSERT INTO proxy_students VALUES ('proxy-1','selective','run-1','2026-01-02');
            """)
        with sqlite3.connect(model_db) as con:
            con.execute("UPDATE preference_model_runs SET manifest_json=?", (
                json.dumps({"derivation": {"kind": "sparse_component", "source_run_id": "old-run"}}),
            ))
        options = _recommendation_options({"limit": ["2"], "policy": ["selective"]})

        def result_for(receipt):
            with sqlite3.connect(model_db) as con:
                con.execute("INSERT OR REPLACE INTO preference_state VALUES (?,?)", (
                    "policy_refresh:selective", json.dumps(receipt),
                ))
            return policy_recommendations(db, model_db, proxy_db, options)

        for receipt in (None, [], {"run_id": "old-run", "result": {"scored_families": 2}},
                        {"run_id": "run-1", "result": {"scored_families": 1}},
                        {"run_id": "run-1", "result": {"scored_families": True}}):
            result = result_for(receipt)
            assert not result["model"]["ready"], receipt
            assert result["model"]["reason"] == "awaiting_complete_policy_refresh"
            assert result["recommendations"] == []

        complete = {"run_id": "run-1", "result": {"scored_families": 2}}
        result = result_for(complete)
        assert result["model"]["ready"] and result["recommendations"]
        # Matching row counts alone do not establish coverage of the current families.
        with sqlite3.connect(model_db) as con:
            con.execute("UPDATE preference_scores SET family_id='obsolete-family' WHERE family_id='family-accounting'")
        result = result_for(complete)
        assert not result["model"]["ready"] and not result["recommendations"]
        with sqlite3.connect(model_db) as con:
            con.execute("DELETE FROM preference_scores WHERE family_id='obsolete-family'")
        result = result_for(complete)
        assert not result["model"]["ready"] and not result["recommendations"]


def test_compare_withholds_incomplete_derived_policy_and_keeps_ready_policy() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        proxy_db = Path(directory) / "jobs-proxy.db"
        seed_families_and_model(db, model_db)
        backfill_locations(db)
        with sqlite3.connect(model_db) as con:
            con.execute("INSERT INTO preference_model_runs SELECT 'run-2',created_at,model_revision,"
                        "text_version,training_examples,manifest_json,artifact_path FROM preference_model_runs")
            con.execute("INSERT INTO preference_scores SELECT 'run-2',family_id,dense_linear_score,"
                        "dense_neighbor_score,sparse_score,final_score,explanation_json,scored_at FROM preference_scores")
            con.execute("UPDATE preference_model_runs SET manifest_json=? WHERE run_id='run-1'", (
                json.dumps({"derivation": {"kind": "sparse_component"}}),
            ))
        with sqlite3.connect(proxy_db) as con:
            con.executescript("""
                CREATE TABLE proxy_runs (run_id TEXT,created_at TEXT);
                CREATE TABLE proxy_students (run_id TEXT,policy_id TEXT,model_run_id TEXT,trained_at TEXT);
                INSERT INTO proxy_runs VALUES ('proxy-1','2026-01-01');
                INSERT INTO proxy_students VALUES ('proxy-1','selective','run-1','2026-01-02');
                INSERT INTO proxy_students VALUES ('proxy-1','broad','run-2','2026-01-02');
            """)
        options = _recommendation_options({"limit": ["2"], "policy": ["compare"]})
        result = policy_recommendations(db, model_db, proxy_db, options)
        assert not result["model"]["ready"]
        assert not result["model"]["policies"]["selective"]["ready"]
        assert result["model"]["policies"]["broad"]["ready"]
        assert result["recommendations"]
        assert {row["policy_id"] for row in result["recommendations"]} == {"broad"}


def test_terminal_feedback_suppresses_family_but_saved_does_not() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "jobs-preference.db"
        seed_families_and_model(db, model_db)
        options = _recommendation_options({"limit": ["2"]})
        save_recommendation_feedback(db, {
            "ats": "ashby", "id": "1", "action": "saved",
        })
        visible = recommendations(db, model_db, options)["recommendations"]
        assert "family-product" in {row["family_id"] for row in visible}
        save_recommendation_feedback(db, {
            "ats": "ashby", "id": "1", "action": "dismissed_preference",
        })
        # A later save does not undo a terminal action in v1.
        save_recommendation_feedback(db, {
            "ats": "ashby", "id": "1", "action": "saved",
        })
        remaining = recommendations(db, model_db, options)["recommendations"]
        assert "family-product" not in {row["family_id"] for row in remaining}


def test_ui_submits_the_expanded_label_shape() -> None:
    root = Path(__file__).parents[1]
    script = (root / "job_search/ranking/web" / "app.js").read_text()
    page = (root / "job_search/ranking/web" / "index.html").read_text()
    for field in (
        "qualification_fit", "primary_reason", "hard_blockers", "skip_reason",
    ):
        assert field in script, f"UI does not submit {field}"
    assert 'label("maybe")' in script
    assert 'label("skipped")' in script
    assert 'id="maybe"' in page
    assert 'id="skip"' in page
    assert 'id="hard-blocker-options"' in page
    assert 'id="stage-progress-bar"' in page
    assert 'id="recommendations-view"' in page
    assert "/api/recommendation-feedback" in script
    # Legacy labeling/recommendation views must not bias judgments with model diagnostics.
    assert "positive_sparse_phrases" not in script
    assert "similar_liked_family_ids" not in script
    assert "score-line" not in script
    for action in (
        "applied", "saved", "dismissed_preference", "blocked", "duplicate",
    ):
        assert action in script
    assert "Ignore whether your résumé is currently competitive" in page
    assert 'id="ats-badge"' not in page and 'id="matched-text"' not in page

    ids = set(re.findall(r'id="([\w-]+)"', page))
    selected_ids = set(re.findall(r'"#([\w-]+)', script))
    assert selected_ids <= ids, f"JavaScript references missing IDs: {selected_ids - ids}"

    chip_values = set(re.findall(r'data-value="([\w-]+)"', page))
    allowed = (
        QUALIFICATION_FITS | PRIMARY_REASONS | HARD_BLOCKERS | SKIP_REASONS
    ) - {""}
    assert chip_values <= allowed, f"UI submits unknown values: {chip_values - allowed}"


def _handler_request(handler_type, method: str, path: str, payload=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    handler = handler_type.__new__(handler_type)
    handler.command = method
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.close_connection = True
    handler.client_address = ("127.0.0.1", 0)
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    getattr(handler, f"do_{method}")()
    raw_headers, raw_body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    status = int(raw_headers.split(b" ", 2)[1])
    return status, raw_body


def test_http_handler_flow_uses_descriptions_and_all_new_fields() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        handler = make_handler(db)
        handler.log_message = lambda self, format, *args: None

        status, body = _handler_request(handler, "GET", "/")
        assert status == 200 and 'id="qualification-options"' in body.decode()

        status, body = _handler_request(handler, "GET", "/app.js")
        assert status == 200 and "qualification_fit" in body.decode()

        status, body = _handler_request(handler, "GET", "/api/job?days=7")
        job = json.loads(body)["job"]
        assert status == 200
        assert job["id"] == "1"
        assert job["description"] == "Build distributed systems."

        payload = {
            "ats": job["ats"],
            "id": job["id"],
            "selection_token": job["selection_token"],
            "interest": "maybe",
            "qualification_fit": "stretch",
            "primary_reason": "growth_scope",
            "hard_blockers": ["location", "travel"],
            "note": "Great role, wrong location",
        }
        status, body = _handler_request(handler, "POST", "/api/label", payload)
        assert status == 200 and json.loads(body)["ok"]

        status, body = _handler_request(handler, "GET", "/api/stats?days=7")
        stats = json.loads(body)
        assert status == 200
        assert stats["maybe"] == 1 and stats["unlabeled"] == 0
        assert stats["program"]["decisive"] == 0
        assert stats["program"]["stage"]["quota"] == 50

        with sqlite3.connect(db) as con:
            stored = con.execute(
                "SELECT interest, qualification_fit, primary_reason, "
                "hard_blockers, program_stage, sample_role, note "
                "FROM job_preferences WHERE ats=? AND job_id=?",
                (job["ats"], job["id"]),
            ).fetchone()
        assert stored == (
            "maybe", "stretch", "growth_scope", '["location", "travel"]',
            1, "training", "Great role, wrong location",
        )

        status, body = _handler_request(handler, "POST", "/api/undo")
        undone = json.loads(body)["undone"]
        assert status == 200 and undone["job_id"] == "1"


def test_http_recommendation_endpoints_use_separate_sidecar_and_limit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = make_database(directory)
        model_db = Path(directory) / "model.db"
        seed_families_and_model(db, model_db)
        handler = make_handler(db, model_db, recommendation_limit=1)
        handler.log_message = lambda self, format, *args: None

        status, body = _handler_request(handler, "GET", "/api/model-status")
        model = json.loads(body)
        assert status == 200 and model["champion_run_id"] == "run-1"

        status, body = _handler_request(handler, "GET", "/api/recommendations")
        result = json.loads(body)
        assert status == 200 and result["options"]["limit"] == 1
        assert len(result["recommendations"]) == 1

        status, body = _handler_request(
            handler, "GET", "/api/recommendations?limit=2&salary_floor=100000"
        )
        result = json.loads(body)
        assert status == 200 and len(result["recommendations"]) == 1
        assert result["recommendations"][0]["salary"]["status"] == "unknown"

        status, body = _handler_request(handler, "GET", "/api/job?days=7")
        label_job = json.loads(body)["job"]
        assert status == 200
        assert "final_score" not in label_job and "score_components" not in label_job

        status, body = _handler_request(handler, "POST", "/api/recommendation-feedback", {
            "ats": "ashby", "id": "1", "action": "saved", "rank": 1,
            "model_run_id": "run-1",
        })
        feedback = json.loads(body)
        assert status == 200 and not feedback["preference_label_changed"]


def test_recent_family_lookup_does_not_scan_the_open_catalog() -> None:
    from job_search.ranking.labeler import _family_members
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE jobs(ats TEXT,id TEXT,description TEXT,publishedAt TEXT,closed_at TEXT,PRIMARY KEY(ats,id));
            CREATE INDEX jobs_closed_at ON jobs(closed_at);
            CREATE TABLE job_family_members(ats TEXT,job_id TEXT,family_id TEXT,PRIMARY KEY(ats,job_id));
            CREATE INDEX job_family_members_family ON job_family_members(family_id);
        """)
        now = datetime.now(timezone.utc).isoformat()
        con.executemany("INSERT INTO jobs VALUES ('ashby',?,?,?,NULL)",
                        [(str(i), 'A substantial description ' * 50, now) for i in range(8000)])
        con.executemany("INSERT INTO job_family_members VALUES ('ashby',?,?)",
                        [(str(i), f'family-{i}') for i in range(8000)])
        con.execute("UPDATE jobs SET closed_at=? WHERE id='1'", (now,))
        con.execute("UPDATE jobs SET publishedAt='2000-01-01' WHERE id='2'")
        con.execute("UPDATE jobs SET description='' WHERE id='3'")
        # Bound SQLite work, rather than relying on machine-dependent wall time.
        # A catalog-wide scan exceeds this budget; indexed identity lookups do not.
        steps = 0
        def budget():
            nonlocal steps
            steps += 100
            return int(steps > 5000)
        con.set_progress_handler(budget, 100)
        result = _family_members(con, [f'family-{i}' for i in range(4)], 1)
        con.set_progress_handler(None, 0)
        assert list(result) == ['family-0']
        assert result['family-0'][0]['id'] == '0'


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} labeler tests)")


if __name__ == "__main__":
    main()
