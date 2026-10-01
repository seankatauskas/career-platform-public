#!/usr/bin/env python3
"""Offline integration tests for the recurring local salary queue."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_search.collection.boards import FIELDS, save
from job_search.inference import GenerationResult
from job_search.salary.enrichment import (
    _merge_ranges,
    enrich_job,
    parse_salary_text,
    prepare_enrichment,
    save_source_ranges,
)
from job_search.salary.llm import HostedSalaryExtractor, activate, retry_failed, run_worker, status
from job_search.salary.model_contract import PROMPT, TEMPLATE, chunk_text


def job(identifier: str, description: str, ats: str = "ashby") -> dict:
    row = {field: "" for field in FIELDS}
    row.update({
        "ats": ats, "company": "acme", "id": identifier, "title": "Engineer",
        "location": "United States", "description": description,
    })
    return row


def mock_salary(amount: int, evidence: str):
    def generate(_: str):
        return json.dumps({"ranges": [{
            "currency": "USD", "period": "year",
            "min_value": amount, "max_value": amount,
            "evidence_text": evidence,
        }]}), {"prompt_tokens": 10, "generation_tokens": 5}
    return generate


class FakeHostedSalaryProvider:
    model_revision = "example/salary-model@immutable-test-revision"
    max_input_tokens = 32_768
    provenance = {
        "provider": "test-cloud",
        "model_revision": model_revision,
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
            json.dumps({"ranges": [{
                "currency": "USD", "period": "year",
                "min_value": 120000, "max_value": 120000,
                "evidence_text": "$120,000",
            }]}),
            {"prompt_tokens": 10, "generation_tokens": 5},
            self.provenance,
        )


class SalaryLlmTests(unittest.TestCase):
    def test_remote_nuextract_preserves_frozen_template_and_instructions(self) -> None:
        provider = FakeHostedSalaryProvider()
        provider.provenance = {**provider.provenance, "model": "numind/NuExtract3"}
        extractor = HostedSalaryExtractor(provider)
        extractor("Annual salary: $120,000")
        self.assertEqual(provider.last_messages, [{"role": "user", "content": "Annual salary: $120,000"}])
        self.assertEqual(provider.last_options["extraction_template"], TEMPLATE)
        self.assertEqual(provider.last_options["extraction_instructions"], PROMPT)
        self.assertEqual(provider.last_options["max_output_tokens"], 512)
        self.assertEqual(provider.last_options["temperature"], 0.0)

    def test_hosted_chunk_budget_counts_json_escaping(self) -> None:
        extractor = HostedSalaryExtractor(FakeHostedSalaryProvider())
        text = '\\n"quoted"\\\\path' * 100
        self.assertEqual(extractor.count_tokens(text), len(json.dumps(text, ensure_ascii=False).encode()))
        self.assertGreater(extractor.count_tokens(text), len(text.encode()))

    def test_explicit_hosted_provider_preserves_revision_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            save(
                [job("base", "No salary")], db, "t0",
                enrichments=[enrich_job("ashby", "base", "No salary")],
            )
            activate(db, no_backfill=True)
            description = "The annual salary is $120,000."
            save(
                [job("remote", description)], db, "t1",
                enrichments=[enrich_job("ashby", "remote", description)],
            )
            provider = FakeHostedSalaryProvider()
            result = run_worker(db, inference_provider=provider)
            self.assertEqual((result["processed"], result["errors"], provider.calls), (1, 0, 1))
            with sqlite3.connect(db) as con:
                model, usage_json = con.execute(
                    "SELECT model,usage_json FROM salary_llm_queue WHERE job_id='remote'"
                ).fetchone()
            self.assertEqual(model, provider.model_revision)
            self.assertEqual(
                json.loads(usage_json)["inference_provenance"]["provider"],
                "test-cloud",
            )
            self.assertEqual(provider.last_options["schema_name"], "salary_extraction")

    def test_activation_retires_legacy_rows_without_queueing_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            baseline = enrich_job("ashby", "old", "Salary is $100,000 annually.")
            save([job("old", "Salary is $100,000 annually.")], db, "t1", enrichments=[baseline])
            with sqlite3.connect(db) as con:
                prepare_enrichment(con)
                legacy = _merge_ranges(parse_salary_text("Salary is $100,000 annually."))
                save_source_ranges(con, "ashby", "old", legacy, {"description_rule"}, "t1")
            result = activate(db, no_backfill=True)
            self.assertEqual(result["retired_description_ranges"], 1)
            self.assertEqual(status(db)["actionable"], 0)
            # Legacy fingerprints may differ solely because the new native-only
            # scan observes a richer raw ATS payload.  Its first observation is
            # still baseline synchronization, not backfill work.
            with sqlite3.connect(db) as con:
                con.execute(
                    "UPDATE job_enrichment SET parser_version='salary-rules-v1',"
                    "source_fingerprint='legacy-payload' WHERE job_id='old'"
                )
            same = enrich_job("ashby", "old", "Salary is $100,000 annually.")
            save([job("old", "Salary is $100,000 annually.")], db, "t2", enrichments=[same])
            self.assertEqual(status(db)["counts"], {})
            # Once synchronized, an actual subsequent posting change is queued.
            changed_description = "Salary is now $101,000 annually."
            changed = enrich_job("ashby", "old", changed_description)
            save([job("old", changed_description)], db, "t3", enrichments=[changed])
            self.assertEqual(status(db)["counts"], {"queued": 1})
            self.assertTrue(activate(db, no_backfill=True)["already_active"])

    def test_new_and_changed_jobs_queue_and_persist_llm_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            save([job("baseline", "No salary here")], db, "t0",
                 enrichments=[enrich_job("ashby", "baseline", "No salary here")])
            activate(db, no_backfill=True)
            description = "The annual salary is $120,000."
            save([job("new", description)], db, "t1",
                 enrichments=[enrich_job("ashby", "new", description)])
            self.assertEqual(status(db)["counts"], {"queued": 1})
            # Repeating an unchanged scan does not duplicate pending work.
            save([job("new", description)], db, "t1-again",
                 enrichments=[enrich_job("ashby", "new", description)])
            self.assertEqual(status(db)["counts"], {"queued": 1})
            outcome = run_worker(
                db, limit=10, generator=mock_salary(120000, "$120,000"),
                count_tokens=lambda value: len(value.split()),
            )
            self.assertEqual((outcome["processed"], outcome["errors"]), (1, 0))
            with sqlite3.connect(db) as con:
                current = con.execute(
                    "SELECT source_type,min_value,max_value,value_kind FROM "
                    "job_compensation_ranges WHERE ats='ashby' AND job_id='new' "
                    "AND removed_at IS NULL"
                ).fetchall()
            self.assertEqual(current, [("llm_description", 120000.0, 120000.0, "exact")])

            changed = "The annual salary is $130,000."
            save([job("new", changed)], db, "t2",
                 enrichments=[enrich_job("ashby", "new", changed)])
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM job_compensation_ranges WHERE job_id='new' "
                    "AND source_type='llm_description' AND removed_at IS NULL"
                ).fetchone()[0], 0)
            self.assertEqual(status(db)["counts"]["queued"], 1)
            run_worker(
                db, limit=10, generator=mock_salary(130000, "$130,000"),
                count_tokens=lambda value: len(value.split()),
            )
            with sqlite3.connect(db) as con:
                values = con.execute(
                    "SELECT min_value,removed_at FROM job_compensation_ranges "
                    "WHERE job_id='new' AND source_type='llm_description' ORDER BY min_value"
                ).fetchall()
            self.assertEqual(values, [(120000.0, "t2"), (130000.0, None)])

            # If a posting cycles back to an earlier fingerprint, its unique queue
            # row is safely re-queued instead of leaving the job without fallback.
            save([job("new", description)], db, "t3",
                 enrichments=[enrich_job("ashby", "new", description)])
            self.assertEqual(status(db)["counts"], {"complete": 1, "queued": 1})

    def test_usable_native_salary_suppresses_queue_and_retires_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            save([job("base", "none")], db, "t0",
                 enrichments=[enrich_job("ashby", "base", "none")])
            activate(db, no_backfill=True)
            description = "The annual salary is $100,000."
            save([job("native", description)], db, "t1",
                 enrichments=[enrich_job("ashby", "native", description)])
            run_worker(
                db, generator=mock_salary(100000, "$100,000"),
                count_tokens=lambda value: len(value.split()),
            )
            raw = {"compensation": {
                "interval": "1 YEAR", "currency": "USD",
                "minValue": 110000, "maxValue": 140000,
            }}
            save([job("native", description)], db, "t2",
                 enrichments=[enrich_job("ashby", "native", description, raw)])
            with sqlite3.connect(db) as con:
                current = con.execute(
                    "SELECT source_type,min_value,max_value FROM job_compensation_ranges "
                    "WHERE job_id='native' AND removed_at IS NULL"
                ).fetchall()
            self.assertEqual(current, [("ats_structured", 110000.0, 140000.0)])

    def test_native_disappearance_queues_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            save([job("base", "none")], db, "t0",
                 enrichments=[enrich_job("ashby", "base", "none")])
            activate(db, no_backfill=True)
            description = "The annual salary is $100,000."
            raw = {"compensation": {
                "interval": "1 YEAR", "currency": "USD",
                "minValue": 100000, "maxValue": 100000,
            }}
            save([job("native", description)], db, "t1",
                 enrichments=[enrich_job("ashby", "native", description, raw)])
            self.assertEqual(status(db)["actionable"], 0)
            save([job("native", description)], db, "t2",
                 enrichments=[enrich_job("ashby", "native", description, {})])
            self.assertEqual(status(db)["counts"], {"queued": 1})
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM job_compensation_ranges WHERE job_id='native' "
                    "AND source_type='ats_structured' AND removed_at IS NULL"
                ).fetchone()[0], 0)

    def test_non_primary_native_component_does_not_suppress_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            save([job("base", "none")], db, "t0",
                 enrichments=[enrich_job("ashby", "base", "none")])
            activate(db, no_backfill=True)
            description = "Base salary is $120,000. Annual bonus is $10,000."
            raw = {"compensation": {
                "compensationTiers": [{"title": "US", "components": [{
                    "compensationType": "Bonus", "interval": "1 YEAR",
                    "currencyCode": "USD", "minValue": 10000, "maxValue": 10000,
                }]}],
            }}
            save([job("bonus", description)], db, "t1",
                 enrichments=[enrich_job("ashby", "bonus", description, raw)])
            self.assertEqual(status(db)["counts"], {"queued": 1})
            run_worker(
                db, generator=mock_salary(120000, "$120,000"),
                count_tokens=lambda value: len(value.split()),
            )
            with sqlite3.connect(db) as con:
                preferred = con.execute(
                    "SELECT source_type FROM job_compensation_ranges WHERE job_id='bonus' "
                    "AND removed_at IS NULL AND is_preferred=1"
                ).fetchall()
            self.assertEqual(preferred, [("llm_description",)])

    def test_closed_job_is_skipped_before_inference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            save([job("base", "none")], db, "t0",
                 enrichments=[enrich_job("ashby", "base", "none")])
            activate(db, no_backfill=True)
            description = "The annual salary is $120,000."
            save([job("closed", description)], db, "t1",
                 enrichments=[enrich_job("ashby", "closed", description)])
            with sqlite3.connect(db) as con:
                con.execute("UPDATE jobs SET closed_at='t2' WHERE id='closed'")
            calls = []
            run_worker(
                db, generator=lambda text: calls.append(text),
                count_tokens=lambda value: len(value.split()),
            )
            self.assertEqual(calls, [])
            self.assertEqual(status(db)["counts"], {"skipped_closed": 1})

    def test_invalid_output_needs_review_and_empty_description_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            save([job("base", "none")], db, "t0",
                 enrichments=[enrich_job("ashby", "base", "none")])
            activate(db, no_backfill=True)
            save(
                [job("invalid", "Salary available."), job("empty", "")], db, "t1",
                enrichments=[
                    enrich_job("ashby", "invalid", "Salary available."),
                    enrich_job("ashby", "empty", ""),
                ],
            )
            run_worker(
                db, generator=mock_salary(100000, "$100,000"),
                count_tokens=lambda value: len(value.split()),
            )
            self.assertEqual(
                status(db)["counts"], {"needs_review": 1, "skipped_empty": 1}
            )

    def test_long_text_chunks_with_overlap_and_no_keyword_prefilter(self) -> None:
        text = "\n\n".join(f"paragraph {index} words here" for index in range(20))
        chunks = chunk_text(text, lambda value: len(value.split()), max_tokens=20, overlap_tokens=5)
        self.assertGreater(len(chunks), 1)
        self.assertIn("paragraph 0", chunks[0])
        self.assertIn("paragraph 19", chunks[-1])
        self.assertTrue(all(len(chunk.split()) <= 20 for chunk in chunks))

    def test_runtime_failures_retry_three_times_then_require_explicit_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            save([job("base", "none")], db, "t0",
                 enrichments=[enrich_job("ashby", "base", "none")])
            activate(db, no_backfill=True)
            description = "The annual salary is $120,000."
            save([job("error", description)], db, "t1",
                 enrichments=[enrich_job("ashby", "error", description)])

            def broken(_: str):
                raise RuntimeError("temporary model failure")

            for attempt in range(3):
                result = run_worker(
                    db, generator=broken,
                    count_tokens=lambda value: len(value.split()),
                )
                self.assertEqual(result["errors"], 1)
                if attempt < 2:
                    with sqlite3.connect(db) as con:
                        con.execute(
                            "UPDATE salary_llm_queue SET next_attempt_at=NULL "
                            "WHERE status='retryable'"
                        )
            self.assertEqual(status(db)["counts"], {"failed": 1})
            self.assertEqual(retry_failed(db), 1)
            self.assertEqual(status(db)["counts"], {"queued": 1})


if __name__ == "__main__":
    unittest.main()
