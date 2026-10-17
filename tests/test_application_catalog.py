"""Scanned metadata wins without changing browser evidence or replay identities."""
import json
import unittest
from dataclasses import asdict

from job_search.application_catalog import repair_preview
from job_search.application_gateway import ApplicationGateway
from job_search.commands import CommandContext, DomainError, Principal
from job_search.contracts import ContractError, JobSnapshot
from tests.test_redesign_extension import PairedExtensionTest
from tests.test_browser_tracking import Catalog, URL, observation


class MutableCatalog(Catalog):
    title = "Software Engineer"
    url = URL
    missing = False
    unavailable = False

    def get_job(self, ats, job_id):
        if self.unavailable:
            raise ContractError("catalog unavailable")
        if self.missing:
            raise ContractError("job was not found")
        return {**super().get_job(ats, job_id), "title": self.title, "jobUrl": self.url}


class ApplicationCatalogTest(PairedExtensionTest):
    def setUp(self):
        super().setUp()
        self.catalog = MutableCatalog()
        self.runtime.job_catalog = self.catalog

    def job(self, application_id):
        with self.runtime.executor.read() as con:
            app = self.runtime.applications.get_application(con, application_id)
            return self.runtime.applications.get_job(con, app["job_id"])

    def preview(self, aid):
        with self.runtime.executor.read() as con:
            return repair_preview(self.runtime.applications, con, self.catalog, aid)

    def repair(self, plan, *, kind="human"):
        payload = {k: plan[k] for k in ("application_id", "expected_version", "snapshot_sha256")}
        return self.runtime.command(CommandContext(Principal("repair", kind, frozenset({"repair_catalog_job"})), "repair:" + plan["application_id"]), "repair_catalog_job", payload)

    def test_catalog_title_and_employer_are_retained_with_original_browser_evidence(self):
        raw = observation(title="Job Application for Software Engineer at Acme", employer="acme")
        aid = self.send(raw)["application_id"]
        job = self.job(aid)
        self.assertEqual((job["title"], job["employer"]), ("Software Engineer", "Acme"))
        self.assertEqual(job["recorded_snapshot"]["job_url"], URL)
        self.assertEqual(self.records("observations")[0]["source"]["title"], raw["title"])
        self.assertEqual(self.records("submissions"), [])
        self.assertTrue(self.runtime.queries.review_queue()["items"])

    def test_catalog_changes_and_outage_do_not_break_queue_replay(self):
        raw = observation(title="Page title")
        first = self.send(raw)
        self.catalog.title = "Changed upstream"
        self.assertEqual(self.send(raw), first)
        self.catalog.unavailable = True
        self.assertEqual(self.send(raw), first)
        with self.assertRaises(DomainError):
            self.send({**raw, "title": "Changed evidence"})
        self.catalog.unavailable = False
        self.send(observation("site_acknowledged", raw["attempt_id"]))
        self.assertEqual(self.job(first["application_id"])["title"], "Software Engineer")

    def test_browser_snapshot_is_available_when_catalog_disappears(self):
        aid = self.send(observation())["application_id"]
        self.catalog.missing = True
        gateway = ApplicationGateway(self.runtime, self.ledger, catalog=self.catalog)
        app = gateway.get_application_timeline(aid)["application"]
        self.assertEqual(app["title_snapshot"], "Software Engineer")
        self.assertEqual(app["job_url_snapshot"], URL)
        self.assertEqual(app["job_metadata_source"], "recorded_snapshot")

    def test_unknown_posting_keeps_fallback_then_captures_later_catalog(self):
        self.catalog.missing = True
        raw = observation(title="Browser fallback", employer="browser employer")
        aid = self.send(raw)["application_id"]
        self.assertEqual(self.job(aid)["title"], "Browser fallback")
        self.assertIsNone(self.job(aid)["recorded_snapshot"])
        self.catalog.missing = False
        self.send(observation("site_acknowledged", raw["attempt_id"]))
        self.assertEqual(self.job(aid)["title"], "Software Engineer")

    def test_wrong_board_or_catalog_identity_rolls_back_all_evidence(self):
        for url in (URL.replace("/acme/", "/other/"),):
            with self.assertRaises(DomainError):
                self.send(observation(page_url=url))
        self.catalog.url = URL.replace("12345", "98765")
        with self.assertRaises(DomainError):
            self.send(observation())
        self.assertEqual(self.records("observations"), [])
        self.assertEqual(self.runtime.queries.list_applications()["items"], [])

    def test_custom_employer_url_is_supported(self):
        self.catalog.url = "https://careers.example.test/opening?gh_jid=12345"
        aid = self.send(observation())["application_id"]
        self.assertEqual(self.job(aid)["recorded_snapshot"]["job_url"], self.catalog.url)

    def test_new_attempt_cannot_change_saved_board_when_catalog_is_missing(self):
        self.send(observation())
        self.catalog.missing = True
        with self.assertRaises(DomainError):
            self.send(observation(page_url=URL.replace("/acme/", "/other/")))
        self.assertEqual(len(self.records("observations")), 1)

    def test_unknown_posting_keeps_original_board_across_attempts(self):
        self.catalog.missing = True
        self.send(observation())
        with self.assertRaises(DomainError):
            self.send(observation(page_url=URL.replace("/acme/", "/other/")))
        self.assertEqual(len(self.records("observations")), 1)

    def test_catalog_outage_is_not_treated_as_missing(self):
        self.catalog.unavailable = True
        raw = observation()
        with self.assertRaises(ContractError):
            self.send(raw)
        self.assertEqual(self.records("observations"), [])
        self.catalog.unavailable = False
        self.assertEqual(self.job(self.send(raw)["application_id"])["title"], "Software Engineer")

    def test_ashby_and_lever_catalog_titles_and_board_checks(self):
        jid = "11111111-2222-3333-4444-555555555555"
        for ats, url in (("ashby", "https://jobs.ashbyhq.com/acme/" + jid),
                         ("lever", "https://jobs.lever.co/acme/" + jid)):
            class OtherCatalog:
                def get_job(self, source, job_id):
                    return {"ats": ats, "id": jid, "title": "Catalog role", "company": "Acme", "jobUrl": url}
            self.runtime.job_catalog = OtherCatalog()
            aid = self.send(observation(page_url=url, title="Application page"))["application_id"]
            self.assertEqual(self.job(aid)["title"], "Catalog role")
            with self.assertRaises(DomainError):
                self.send(observation(page_url=url.replace("/acme/", "/other/")))

    def test_explicit_saved_snapshot_and_recommendation_are_preserved(self):
        snapshot = asdict(JobSnapshot("greenhouse", "12345", "", "Original reviewed role", "Original company", "acme", URL))
        self.runtime.command(CommandContext(Principal("owner", "human", frozenset({"*"})), "save"), "save_job",
            {"job_source": {"source": "greenhouse", "source_id": "12345", "title": snapshot["title"], "employer": snapshot["employer"]},
             "job_snapshot": snapshot, "recommendation": {"policy_id": "curated"}})
        aid = self.send(observation())["application_id"]
        job = self.job(aid)
        self.assertEqual(job["recorded_snapshot"], snapshot)
        self.assertEqual(job["recorded_snapshots"][0]["recommendation"], {"policy_id": "curated"})

    def test_repair_retains_identity_activity_versions_evidence_and_is_replayable(self):
        self.catalog.missing = True
        raw = observation(title="Job Application for Software Engineer at Acme")
        aid = self.send(raw)["application_id"]
        with self.runtime.executor.read() as con:
            before = self.runtime.applications.get_application(con, aid)
        evidence = self.records("observations")
        reviews = self.runtime.queries.review_queue()["items"]
        self.catalog.missing = False
        plan = self.preview(aid)
        self.assertEqual(plan["status"], "ready")
        result = self.repair(plan)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(self.repair(plan), result)
        self.assertEqual(self.records("observations"), evidence)
        self.assertEqual(self.runtime.queries.review_queue()["items"], reviews)
        with self.runtime.executor.read() as con:
            self.assertEqual(self.runtime.applications.get_application(con, aid), before)
            history = con.execute("SELECT after_json FROM command_history WHERE operation='capture_catalog_snapshot' AND record_id=?", (aid,)).fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0][0])["snapshot"]["title"], "Software Engineer")

    def test_repair_rejects_changed_catalog_and_worker_authority(self):
        self.catalog.missing = True
        aid = self.send(observation())["application_id"]
        self.catalog.missing = False
        plan = self.preview(aid)
        with self.assertRaises(DomainError):
            self.repair(plan, kind="worker")
        self.catalog.title = "Different role"
        with self.assertRaises(DomainError):
            self.repair(plan)
        self.assertIsNone(self.job(aid)["recorded_snapshot"])

    def test_gateway_catalog_fallback_precedes_unverified_browser_title(self):
        self.catalog.missing = True
        aid = self.send(observation(title="Job Application for Software Engineer at Acme"))["application_id"]
        self.catalog.missing = False
        gateway = ApplicationGateway(self.runtime, self.ledger, catalog=self.catalog)
        app = gateway.get_application_timeline(aid)["application"]
        self.assertEqual(app["title_snapshot"], "Software Engineer")
        self.assertEqual(app["employer_snapshot"], "Acme")


if __name__ == "__main__":
    unittest.main()
