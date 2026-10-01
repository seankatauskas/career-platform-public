#!/usr/bin/env python3

import json
import sqlite3
import tempfile
from pathlib import Path

from job_search.collection.dedupe import prepare_families
from job_search.inference import GenerationResult
from job_search.collection.locations import backfill as backfill_locations
from job_search.ranking.proxy import (
    ProxyError,
    derive_policies,
    estimate,
    load_profile,
    _candidate_rows,
    _snapshot_rows,
    _usable_passive_feedback,
    load_passive_feedback_examples,
    load_proxy_examples,
    prepare_run,
    proxy_text,
    retry_failed,
    run_teacher,
    status,
    validate_teacher_result,
)


TEST_PROFILE = {
    "profile_id": "test-software-engineer",
    "experience_years": 1.5,
    "current_employer": "consumer technology company",
    "education": "undergraduate computer science degree",
    "capability_assumption": "top percentile at current experience level",
    "role_scope": ["software", "platform", "mobile", "ai", "ml"],
    "max_seniority": "senior",
    "country_required": "US",
    "preferred_metros": [
        "sf_bay_area",
        "new_york_city",
        "seattle",
        "boston",
        "austin",
        "los_angeles",
        "chicago",
    ],
    "remote_policy": "city_preferred",
    "missing_salary_policy": "neutral",
    "compensation": {"low_usd": 150000, "high_usd": 300000},
}


def _profile(directory: str) -> Path:
    path = Path(directory) / "profile.json"
    path.write_text(json.dumps(TEST_PROFILE), encoding="utf-8")
    return path


GOOD_RESULT = {
    "software_role": "core_swe",
    "specialties": ["platform"],
    "seniority": "mid",
    "software_relevance": 4,
    "interesting_work": 4,
    "qualification_fit": 3,
    "confidence": 4,
    "reason_codes": ["hands_on_building"],
    "positive_evidence": [
        {
            "dimension": "interesting_work",
            "quote": "Build distributed systems",
        }
    ],
    "negative_evidence": [],
}


class FakeTeacher:
    model_revision = "fake-teacher@1"

    def __init__(self, invalid_first: bool = False) -> None:
        self.calls = 0
        self.invalid_first = invalid_first

    def generate(self, prompt: str):
        self.calls += 1
        if self.invalid_first and self.calls == 1:
            return "{}", {"prompt_tokens": 1, "generation_tokens": 1}
        return json.dumps(GOOD_RESULT), {"prompt_tokens": 10, "generation_tokens": 20}


class FakeHostedTeacherProvider:
    model_revision = "example/teacher@immutable-test-revision"
    generation_identity = model_revision + "#provider-first"
    max_input_tokens = 32_768
    provenance = {
        "provider": "test-cloud",
        "model_revision": model_revision,
        "generation_identity": generation_identity,
        "deployment_revision": "worker@immutable-test-revision",
    }

    def __init__(self) -> None:
        self.calls = 0

    def count_tokens_upper_bound(self, text: str) -> int:
        return len(text.encode("utf-8"))

    def generate(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = messages
        self.last_options = kwargs
        return GenerationResult(
            json.dumps(GOOD_RESULT),
            {"prompt_tokens": 10, "generation_tokens": 20},
            self.provenance,
        )


def _make_source(directory: str, count: int = 20) -> Path:
    db = Path(directory) / "jobs.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE jobs (ats TEXT,id TEXT,company TEXT,title TEXT,department TEXT,"
            "team TEXT,employmentType TEXT,location TEXT,isRemote TEXT,workplaceType TEXT,"
            "publishedAt TEXT,jobUrl TEXT,description TEXT,closed_at TEXT,PRIMARY KEY(ats,id))"
        )
        rows = []
        for index in range(count):
            if index % 4 == 0:
                title = "Senior Software Engineer"
            elif index % 4 == 1:
                title = "Platform Engineer"
            elif index % 4 == 2:
                title = "Solutions Architect"
            else:
                title = "Product Analyst"
            rows.append(
                (
                    "ashby",
                    str(index),
                    f"company-{index}",
                    title,
                    "Engineering",
                    "Core",
                    "Full-time",
                    "New York, NY",
                    "0",
                    "Hybrid",
                    "2099-01-01T00:00:00Z",
                    f"https://example.test/{index}",
                    f"Build distributed systems for unique service number {index}. "
                    * 30,
                    None,
                )
            )
        con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute(
            "CREATE TABLE job_compensation_ranges (ats TEXT,job_id TEXT,evidence_text TEXT,"
            "removed_at TEXT)"
        )
        con.execute(
            "INSERT INTO job_compensation_ranges VALUES (?,?,?,NULL)",
            ("ashby", "0", "Base salary $140,000 to $180,000."),
        )
        con.execute(
            "UPDATE jobs SET description=description || ? WHERE id='0'",
            (" Base salary $140,000 to $180,000.",),
        )
    prepare_families(db)
    backfill_locations(db)
    return db


def test_profile_and_policy_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        profile, fingerprint = load_profile(_profile(directory))
        assert profile["experience_years"] == 1.5 and len(fingerprint) == 64
    policies = derive_policies(GOOD_RESULT)
    assert policies["selective"]["label"] == "interested"
    assert policies["broad"]["label"] == "interested"
    weak = dict(
        GOOD_RESULT, software_relevance=2, interesting_work=2, qualification_fit=2
    )
    policies = derive_policies(weak)
    assert policies["selective"]["label"] == "not_interested"
    assert policies["broad"]["label"] == "interested"
    staff = derive_policies(dict(GOOD_RESULT, seniority="staff_plus"))
    assert staff["selective"]["label"] == staff["broad"]["label"] == "not_interested"
    for relevance in range(5):
        for interesting in range(5):
            for qualification in range(5):
                values = dict(
                    GOOD_RESULT,
                    software_relevance=relevance,
                    interesting_work=interesting,
                    qualification_fit=qualification,
                )
                exhaustive = derive_policies(values)
                if exhaustive["selective"]["label"] == "interested":
                    assert exhaustive["broad"]["label"] == "interested"


def test_validation_and_proxy_text_redaction() -> None:
    source = "Engineer\nBuild distributed systems and services."
    assert validate_teacher_result(GOOD_RESULT, source) == []
    bad = dict(GOOD_RESULT)
    bad["positive_evidence"] = [
        {
            "dimension": "interesting_work",
            "quote": "invented evidence",
        }
    ]
    assert "not in source" in validate_teacher_result(bad, source)[0]
    cleaned = proxy_text(
        ("Build systems and reliable services. " * 20)
        + "Salary is $200,000. Equal Opportunity Employer legal text.",
        ["Salary is $200,000."],
    )
    assert "$200,000" not in cleaned and "Equal Opportunity" not in cleaned

    with tempfile.TemporaryDirectory() as directory:
        source_db = _make_source(directory)
        row = next(row for row in _candidate_rows(source_db, 1) if row["job_id"] == "0")
        snapshot = _snapshot_rows(source_db, [row])[0]
        assert "$140,000" not in snapshot["semantic_text"]


def test_prepare_is_deterministic_grouped_and_resumable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = _make_source(directory)
        proxy = Path(directory) / "proxy.db"
        profile = _profile(directory)
        first = prepare_run(source, proxy, profile, 10, 2, 7)
        second = prepare_run(source, proxy, profile, 10, 2, 7)
        assert first["run"]["run_id"] == second["run"]["run_id"]
        assert first["queue"] == {"audit:pending": 2, "training:pending": 8}
        with sqlite3.connect(proxy) as con:
            assert (
                con.execute(
                    "SELECT COUNT(*) FROM (SELECT template_cluster_id FROM proxy_queue "
                    "GROUP BY template_cluster_id HAVING COUNT(*)>1)"
                ).fetchone()[0]
                == 0
            )

        teacher = FakeTeacher(invalid_first=True)
        result = run_teacher(proxy, profile, "training", 1, teacher=teacher)
        assert result["complete"] == 1 and teacher.calls == 2
        assert status(proxy)["queue"] == {
            "audit:pending": 2,
            "training:complete": 1,
            "training:pending": 7,
        }
        local_estimate = estimate(proxy, "training")
        assert local_estimate["pending_jobs"] == 7
        assert local_estimate["marginal_cost_usd"] == 0.0
        assert (
            estimate(proxy, "training", remote_inference=True)["marginal_cost_usd"]
            is None
        )
        run_teacher(proxy, profile, "training", 7, teacher=FakeTeacher())
        selective = load_proxy_examples(proxy, first["run"]["run_id"], "selective")
        broad = load_proxy_examples(proxy, first["run"]["run_id"], "broad")
        assert len(selective) == len(broad) == 8
        assert selective[0].document.description_text
        assert all(example.sample_weight == 1.0 for example in selective)
        assert retry_failed(proxy)["retried"] == 0


def test_explicit_hosted_teacher_preserves_revision_and_provenance() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = _make_source(directory)
        proxy = Path(directory) / "proxy.db"
        profile = _profile(directory)
        provider = FakeHostedTeacherProvider()
        prepared = prepare_run(
            source,
            proxy,
            profile,
            10,
            2,
            13,
            provider.generation_identity,
        )
        result = run_teacher(
            proxy,
            profile,
            "training",
            1,
            inference_provider=provider,
        )
        assert result["complete"] == 1 and provider.calls == 1
        with sqlite3.connect(proxy) as con:
            model_revision, usage_json = con.execute(
                "SELECT model_revision,usage_json FROM proxy_predictions"
            ).fetchone()
        assert model_revision == provider.generation_identity
        assert (
            json.loads(usage_json)["inference_provenance"]["provider"] == "test-cloud"
        )
        assert prepared["run"]["teacher_model"] == provider.generation_identity
        assert provider.last_options["schema_name"] == "preference_teacher"


def test_remote_teacher_run_identity_rejects_deployment_swap() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = _make_source(directory)
        proxy = Path(directory) / "proxy.db"
        profile = _profile(directory)
        first = FakeHostedTeacherProvider()
        prepared = prepare_run(
            source,
            proxy,
            profile,
            10,
            2,
            14,
            first.generation_identity,
        )
        changed = FakeHostedTeacherProvider()
        changed.generation_identity = changed.model_revision + "#provider-second"
        assert changed.model_revision == first.model_revision
        try:
            run_teacher(
                proxy,
                profile,
                "training",
                1,
                prepared["run"]["run_id"],
                inference_provider=changed,
            )
            raise AssertionError("deployment-swapped teacher reused a prepared run")
        except ProxyError as exc:
            assert "does not match prepared run" in str(exc)

        replacement = prepare_run(
            source,
            proxy,
            profile,
            10,
            2,
            14,
            changed.generation_identity,
        )
        assert replacement["run"]["run_id"] != prepared["run"]["run_id"]


def test_audit_is_hidden_until_students_are_fit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = _make_source(directory)
        proxy = Path(directory) / "proxy.db"
        profile = _profile(directory)
        prepare_run(source, proxy, profile, 10, 2, 9)
        try:
            run_teacher(proxy, profile, "audit", 1, teacher=FakeTeacher())
            raise AssertionError("audit teacher ran before students were frozen")
        except ProxyError as exc:
            assert "fit both students" in str(exc)


def test_passive_actions_become_weighted_examples_only_after_minimum_coverage() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "feedback.db"
        with sqlite3.connect(db) as con:
            con.execute(
                "CREATE TABLE recommendation_feedback (feedback_id INTEGER PRIMARY KEY,"
                "ats TEXT,job_id TEXT,"
                "family_id TEXT,action TEXT,implicit_weight REAL,title_snapshot TEXT,"
                "description_snapshot TEXT,metadata_json TEXT,template_cluster_id TEXT,"
                "leakage_group_id TEXT,source_fingerprint TEXT,created_at TEXT)"
            )
            for index in range(10):
                positive = index < 5
                con.execute(
                    "INSERT INTO recommendation_feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        index + 1,
                        "ashby",
                        str(index),
                        f"family-{index}",
                        "applied" if positive else "dismissed_preference",
                        2.0 if positive else -2.0,
                        f"Engineer {index}",
                        f"Build unique system {index}.",
                        "{}",
                        f"tpl-{index}",
                        f"leak-{index}",
                        f"fp-{index}",
                        f"2026-01-{index + 1:02d}",
                    ),
                )
            con.execute(
                "INSERT INTO recommendation_feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    11,
                    "ashby",
                    "9",
                    "family-9",
                    "saved",
                    0.5,
                    "Engineer 9",
                    "Build unique system 9.",
                    "{}",
                    "tpl-9",
                    "leak-9",
                    "fp-9",
                    "2026-02-01",
                ),
            )
        examples = load_passive_feedback_examples(db)
        usable = _usable_passive_feedback(examples)
        assert len(usable) == 10 and {example.target for example in usable} == {0, 1}
        assert {example.sample_weight for example in usable} == {2.0}
        assert (
            next(
                example
                for example in usable
                if example.document.family_id == "family-9"
            ).target
            == 0
        )


def main() -> None:
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} preference-proxy tests)")


if __name__ == "__main__":
    main()
