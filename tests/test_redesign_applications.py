"""Applications invariants through public operations and real command transactions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from job_search.commands import CommandContext, CommandExecutor, DomainError, Principal
from job_search.applications.api import ApplicationOperations, SCHEMA


class ApplicationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = "2026-10-09T12:00:00Z"
        self.executor = CommandExecutor(Path(self.tmp.name) / "state.db", {"applications": SCHEMA}, clock=lambda: self.clock)
        self.api = ApplicationOperations()
        self.principal = Principal("owner", "human", frozenset({"*"}))
        self.sequence = 0
        self.source = {"source": "greenhouse", "source_id": "job-1", "employer": "Example", "title": "Engineer"}

    def run_command(self, operation, payload, *, key=None, origin="direct", causation_id=None):
        self.sequence += 1
        context = CommandContext(self.principal, key or str(self.sequence), origin, causation_id=causation_id)
        return self.executor.run(context, operation, payload, lambda tx: getattr(self.api, operation)(tx, payload))

    def app(self):
        return self.run_command("save_job", {"job_source": self.source})

    def get(self, kind, identifier):
        with self.executor.read() as con:
            return self.api.get_record(con, kind, identifier)

    def stage(self, identifier):
        with self.executor.read() as con:
            return self.api.stage(con, identifier)["stage"]

    def test_first_activity_concurrency_and_reads(self):
        with self.executor.read() as con:
            self.assertEqual(self.api.list_applications(con)["items"], [])
        def add(i):
            payload = {"job_source": self.source, "text": str(i)}
            context = CommandContext(self.principal, "note-" + str(i))
            return self.executor.run(context, "add_note", payload, lambda tx: self.api.add_note(tx, payload))
        with ThreadPoolExecutor(max_workers=4) as pool:
            notes = list(pool.map(add, range(8)))
        self.assertEqual(len({note["application_id"] for note in notes}), 1)
        with self.executor.read() as con:
            apps = self.api.list_applications(con)["items"]
            self.assertEqual(len(apps), 1)
            self.assertEqual(len(self.api.list_records(con, apps[0]["id"], "notes")["items"]), 8)

    def test_invalid_first_action_leaves_no_container(self):
        with self.assertRaises(DomainError):
            self.run_command("create_task", {"job_source": self.source, "kind": "bogus", "description": "No"})
        with self.executor.read() as con:
            self.assertEqual(self.api.list_applications(con)["items"], [])
        with self.assertRaises(DomainError):
            self.run_command("create_task", {"job_source": self.source, "kind": "complete_assessment", "description": "Portal assessment", "completion_rule": "verified_send"})

    def test_browser_evidence_is_not_confirmation_and_preserves_exact_bytes(self):
        payload = {"job_source": self.source, "device_id": "device", "observation_id": "source-1", "attempt_ref": "attempt-1", "activity": "website_acknowledgment", "answers": {"why": " e\u0301\n "}, "documents": [{"hash": "original", "selection_observed": True}]}
        observed = self.run_command("record_browser_observation", payload)
        self.assertEqual(self.stage(observed["application_id"]), "tracking")
        self.assertEqual(self.run_command("record_browser_observation", payload)["id"], observed["id"])
        self.assertEqual(observed["answers"], payload["answers"])
        with self.assertRaises(DomainError):
            self.run_command("record_browser_observation", {**payload, "answers": {"why": " é\n "}})
        appid = observed["application_id"]
        attempt = self.run_command("record_submission", {"application_id": appid, "answers": payload["answers"], "documents": payload["documents"]})
        self.assertEqual(self.stage(appid), "submitted")
        confirmed = self.run_command("confirm_submission", {"submission_id": attempt["id"], "expected_version": 1, "evidence": [{"owner": "correspondence", "source_id": "mail-1"}]})
        self.assertEqual(confirmed["answers"], payload["answers"])
        self.assertEqual(self.stage(appid), "active")
        self.run_command("confirm_submission", {"submission_id": attempt["id"], "expected_version": 2, "evidence": [{"owner": "applications", "source_id": observed["id"]}]})
        with self.executor.read() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM app_feedback").fetchone()[0], 1)

    def test_interview_reschedule_updates_task_and_reminders_atomically(self):
        app = self.app()
        payload = {"application_id": app["id"], "status": "scheduled", "start_at": "2026-10-12T13:00:00Z", "end_at": "2026-10-12T14:00:00Z", "timezone": "America/Chicago", "create_task": True, "reminders_enabled": True}
        interview = self.run_command("schedule_interview", payload)
        changed = {**payload, "interview_id": interview["id"], "expected_version": 1, "start_at": "2026-10-13T13:00:00Z", "end_at": "2026-10-13T14:00:00Z", "create_task": False}
        with self.executor.read() as con:
            changed["expected_related_versions"] = self.api.preview_interview_change(con, interview["id"])["expected_related_versions"]
        def fail(tx):
            self.api.reschedule_interview(tx, changed)
            raise RuntimeError("injected after reminders")
        with self.assertRaises(RuntimeError):
            self.executor.run(CommandContext(self.principal, "failure"), "reschedule_interview", changed, fail)
        self.assertEqual(self.get("interviews", interview["id"])["version"], 1)
        updated = self.run_command("reschedule_interview", changed)
        self.assertEqual(updated["version"], 2)
        with self.executor.read() as con:
            tasks = self.api.list_records(con, app["id"], "tasks")["items"]
            self.assertEqual(tasks[0]["due_at"], changed["start_at"])
            reminders = self.api.list_records(con, app["id"], "reminders")["items"]
            self.assertEqual([r["status"] for r in reminders].count("pending"), 2)
            self.assertEqual([r["status"] for r in reminders].count("cancelled"), 2)
        self.clock = "2026-10-20T12:00:00Z"
        self.assertEqual(self.get("tasks", tasks[0]["id"])["status"], "open")

    def browser(self, identifier, *, activity="submission_attempt", attempt="attempt", source=None, job_source=None):
        return self.run_command("record_browser_observation", {
            "job_source": job_source or self.source, "device_id": "paired-device",
            "observation_id": identifier, "attempt_ref": attempt, "activity": activity,
            "source": source or {}})

    def test_browser_attempt_is_atomic_across_jobs_and_observation_ids(self):
        original = self.browser("original")
        other = {**self.source, "source_id": "different-job"}
        with self.assertRaises(DomainError) as error:
            self.browser("late", activity="answer_capture", job_source=other)
        self.assertEqual(error.exception.code, "invalid_input")
        with self.executor.read() as con:
            self.assertEqual(len(self.api.list_applications(con)["items"]), 1)
            attempt = self.api.get_browser_attempt(con, "paired-device", "attempt")
            self.assertEqual(attempt["application_id"], original["application_id"])
            self.assertEqual([o["id"] for o in attempt["observations"]], [original["id"]])
            self.assertEqual(attempt["job"]["sources"], [{"source": "greenhouse", "source_id": "job-1"}])
            self.assertIsNone(self.api.get_browser_attempt(con, "missing-device", "attempt"))

    def test_paired_attempt_requires_start_and_stable_board_identity(self):
        identity = {"ats": "greenhouse", "job_id": "job-1", "board": "example", "canonical_url": "https://example.invalid/job"}
        source = {"adapter": "paired_extension_v1", "identity": identity}
        with self.assertRaises(DomainError) as error:
            self.browser("early-ack", activity="website_acknowledgment", source=source)
        self.assertEqual(error.exception.code, "dependency_unresolved")
        first = self.browser("start", source={**source, "kind": "attempted"})
        signal = self.browser("signal", activity="submission_signal", source={**source, "kind": "request_sent"})
        self.assertEqual(signal["activity"], "submission_signal")
        self.assertEqual(self.stage(first["application_id"]), "tracking")
        with self.assertRaises(DomainError):
            self.browser("wrong-board", source={**source, "identity": {**identity, "board": "another"}})
        with self.assertRaises(DomainError):
            self.browser("lost-identity")

    def test_late_answers_are_exact_and_only_attached_by_review(self):
        observed = self.browser("start")
        evidence = [{"owner": "applications", "source_id": observed["id"]}]
        submission = self.run_command("confirm_submission", {"application_id": observed["application_id"], "answers": {"legacy": " exact\n"}, "documents": [{"hash": "original"}], "evidence": evidence})
        snapshots = [{"consent": False, "multi": [" a ", "e\u0301", 7], "empty": None}, " text\n", False, ["x", True], None]
        for index, snapshot in enumerate(snapshots):
            capture = self.browser("capture-" + str(index), activity="answer_capture", source={"answer_snapshot": snapshot})
            self.assertEqual(self.get("submissions", submission["id"])["version"], index + 1)
            updated = self.run_command("attach_submission_answers", {"submission_id": submission["id"], "expected_version": index + 1, "observation_id": capture["id"]})
            self.assertEqual(updated["answer_snapshots"][-1], {"observation_id": capture["id"], "snapshot": snapshot})
            repeated = self.run_command("attach_submission_answers", {"submission_id": submission["id"], "expected_version": index + 1, "observation_id": capture["id"]})
            self.assertEqual(repeated, updated)
            self.assertEqual(updated["answers"], submission["answers"])
            self.assertEqual(updated["documents"], submission["documents"])
        with self.executor.read() as con:
            attempt = self.api.get_browser_attempt(con, "paired-device", "attempt", limit=1)
            self.assertTrue(attempt["coverage"]["truncated"])
            self.assertTrue(attempt["has_answer_snapshot"])
            self.assertIsNone(attempt["latest_answer_snapshot"])
            self.assertEqual(attempt["latest_answer_observation_id"], capture["id"])
            self.assertEqual(con.execute("SELECT count(*) FROM app_feedback").fetchone()[0], 1)
            affected = self.api.affected_by_evidence(con, submission["application_id"], capture["id"])
            self.assertIn(submission["id"], {item["id"] for item in affected["items"]})
        wrong = self.browser("other-capture", attempt="other", activity="answer_capture", source={"answer_snapshot": "wrong"})
        with self.assertRaises(DomainError):
            self.run_command("attach_submission_answers", {"submission_id": submission["id"], "expected_version": updated["version"], "observation_id": wrong["id"]})
        fresh = self.browser("fresh-capture", activity="answer_capture", source={"answer_snapshot": "new"})
        with self.assertRaises(DomainError) as error:
            self.run_command("attach_submission_answers", {"submission_id": submission["id"], "expected_version": 1, "observation_id": fresh["id"]})
        self.assertEqual(error.exception.code, "version_conflict")

    def test_one_current_submission_per_preserved_browser_attempt(self):
        observed = self.browser("start")
        payload = {"application_id": observed["application_id"], "evidence": [{"owner": "applications", "source_id": observed["id"]}]}
        first = self.run_command("record_submission", payload)
        ack = self.browser("ack", activity="website_acknowledgment")
        corroboration = {**payload, "evidence": [{"owner": "applications", "source_id": ack["id"], "device_id": "paired-device", "attempt_ref": "attempt"}]}
        with self.assertRaises(DomainError) as error:
            self.run_command("confirm_submission", corroboration)
        self.assertEqual(error.exception.code, "version_conflict")
        accepted = self.run_command("confirm_submission", {**corroboration, "submission_id": first["id"], "expected_version": 1})
        self.assertEqual(accepted["status"], "confirmed")
        with self.assertRaises(DomainError):
            self.run_command("record_submission", {**payload, "evidence": [{"owner": "applications", "source_id": ack["id"], "attempt_ref": "forged"}]})

    def test_application_identity_query_includes_transitive_aliases_and_reports_truncation(self):
        apps = [self.run_command("save_job", {"job_source": {"source": "test", "source_id": str(i)}}) for i in range(3)]
        for source, target in zip(apps, apps[1:]):
            with self.executor.read() as con:
                preview = self.api.preview_combination(con, source["id"], target["id"])
                source_version = self.api.get_application(con, source["id"])["version"]
                target_version = self.api.get_application(con, target["id"])["version"]
            self.run_command("combine_jobs", {"source_application_id": source["id"], "target_application_id": target["id"], "source_version": source_version, "target_version": target_version, "disposition": "open", "reason": "Same job", "expected_records": preview["expected_records"]})
        with self.executor.read() as con:
            result = self.api.application_identities(con, apps[0]["id"])
            self.assertEqual(set(result["items"]), {app["id"] for app in apps})
            self.assertFalse(result["truncated"])
            bounded = self.api.application_identities(con, apps[0]["id"], limit=1)
            self.assertEqual(len(bounded["items"]), 1)
            self.assertTrue(bounded["truncated"])

    def test_snooze_does_not_change_deadline(self):
        task = self.run_command("create_task", {"job_source": self.source, "kind": "reply", "description": "Reply", "due_at": "2026-10-10T12:00:00Z"})
        after = self.run_command("snooze_task", {"task_id": task["id"], "expected_version": 1, "until": "2026-10-10T14:00:00Z"})
        self.assertEqual(after["due_at"], task["due_at"])
        self.assertEqual(after["status"], "open")

    def test_reschedule_rejects_changed_or_unreviewed_dependent_task(self):
        app = self.app()
        interview = self.run_command("schedule_interview", {"application_id": app["id"], "create_task": True})
        payload = {"interview_id": interview["id"], "expected_version": 1, "status": "scheduled", "start_at": "2026-10-12T13:00:00Z", "end_at": "2026-10-12T14:00:00Z", "timezone": "UTC"}
        with self.assertRaises(DomainError):
            self.run_command("reschedule_interview", payload)
        with self.executor.read() as con:
            preview = self.api.preview_interview_change(con, interview["id"])
            task = preview["records"][0]["record"]
        payload["expected_related_versions"] = preview["expected_related_versions"]
        self.run_command("snooze_task", {"task_id": task["id"], "expected_version": 1, "until": "2026-10-11T12:00:00Z"})
        with self.assertRaises(DomainError):
            self.run_command("reschedule_interview", payload)
        self.assertEqual(self.get("interviews", interview["id"])["status"], "requested")

    def test_container_ensure_does_not_record_explicit_save(self):
        self.run_command("ensure_application", {"job_source": self.source})
        with self.executor.read() as con:
            operations = [row[0] for row in con.execute("SELECT operation FROM command_history")]
            self.assertEqual(operations, ["create_application"])

    def test_close_and_reopen_preserve_facts_without_reviving_work(self):
        app = self.app()
        interview = self.run_command("schedule_interview", {"application_id": app["id"], "create_task": True})
        offer = self.run_command("record_offer", {"application_id": app["id"], "terms": {"salary": "unknown"}})
        with self.executor.read() as con:
            expected = self.api.preview_closure(con, app["id"])["expected_records"]
        closed = self.run_command("close_application", {"application_id": app["id"], "expected_version": 1, "outcome": "withdrawn", "reason": "User decision", "expected_records": expected})
        self.assertEqual(self.get("interviews", interview["id"])["status"], "requested")
        self.assertEqual(self.get("offers", offer["id"])["status"], "offered")
        self.assertEqual(self.stage(app["id"]), "closed")
        opened = self.run_command("reopen_application", {"application_id": app["id"], "expected_version": closed["version"], "reason": "Try again"})
        self.assertEqual(opened["pursuit_no"], 2)
        self.assertEqual(self.stage(app["id"]), "tracking")
        with self.executor.read() as con:
            self.assertEqual(self.api.list_records(con, app["id"], "tasks", current_only=True)["items"], [])

    def test_closure_requires_all_current_obligations_at_reviewed_versions(self):
        app = self.app()
        first = self.run_command("create_task", {"application_id": app["id"], "kind": "reply", "description": "First"})
        with self.executor.read() as con:
            preview = self.api.preview_closure(con, app["id"])
        payload = {"application_id": app["id"], "expected_version": app["version"], "reason": "Stop", "expected_records": preview["expected_records"]}
        self.run_command("create_task", {"application_id": app["id"], "kind": "reply", "description": "New independent request"})
        with self.assertRaises(DomainError):
            self.run_command("close_application", payload)
        self.assertEqual(self.get("tasks", first["id"])["status"], "open")
        with self.executor.read() as con:
            self.assertEqual(self.api.get_application(con, app["id"])["disposition"], "open")

    def test_correction_detects_independent_edits_and_preserves_other_records(self):
        app = self.app()
        task = self.run_command("record_request", {"application_id": app["id"], "kind": "reply", "description": "Reply", "origin_ref": "finding-1"}, causation_id="proposal-1")
        self.run_command("add_note", {"application_id": app["id"], "text": "independent note"})
        self.run_command("snooze_task", {"task_id": task["id"], "expected_version": 1, "until": "2026-10-10T13:00:00Z"})
        with self.executor.read() as con:
            causal = self.api.causal_records(con, app["id"], "proposal-1")
            self.assertTrue(causal[0]["independently_edited"])
        with self.assertRaises(DomainError):
            self.run_command("correct_progress", {"application_id": app["id"], "reason": "Wrong source", "corrections": [{"kind": "tasks", "id": task["id"], "expected_version": 1, "resolution": "retract"}]})
        self.run_command("correct_progress", {"application_id": app["id"], "reason": "Wrong source", "corrections": [{"kind": "tasks", "id": task["id"], "expected_version": 2, "resolution": "keep"}]})
        self.assertEqual(self.get("tasks", task["id"])["status"], "open")

    def test_offer_decline_is_not_application_closure_and_versions_are_relevant(self):
        app = self.app()
        first = self.run_command("record_offer", {"application_id": app["id"], "terms": {"compensation": None}})
        self.run_command("record_offer", {"application_id": app["id"], "terms": {"compensation": 100}})
        self.run_command("add_note", {"application_id": app["id"], "text": "Does not stale an offer decision"})
        self.run_command("decide_offer", {"offer_id": first["id"], "expected_version": 1, "status": "declined", "reason": "Decline this offer"})
        self.assertEqual(self.stage(app["id"]), "offer")
        with self.executor.read() as con:
            self.assertEqual(self.api.get_application(con, app["id"])["disposition"], "open")
        with self.assertRaises(DomainError):
            self.run_command("decide_offer", {"offer_id": first["id"], "expected_version": 1, "status": "accepted", "reason": "Stale"})

    def test_requested_interview_cancellation_and_complete_stage_precedence(self):
        app = self.app()
        self.run_command("record_submission", {"application_id": app["id"]})
        self.assertEqual(self.stage(app["id"]), "submitted")
        self.run_command("record_assessment", {"application_id": app["id"], "description": "Portal exercise", "deadline_text": "Friday", "create_task": True})
        self.assertEqual(self.stage(app["id"]), "active")
        interview = self.run_command("schedule_interview", {"application_id": app["id"]})
        self.assertEqual(self.stage(app["id"]), "interviewing")
        self.run_command("cancel_interview", {"interview_id": interview["id"], "expected_version": 1, "reason": "Employer cancellation", "cancellation_kind": "employer_cancelled"})
        self.assertEqual(self.stage(app["id"]), "active")

    def test_invalid_interval_missing_timezone_and_passive_observation_rejected(self):
        app = self.app()
        for fields in ({"status": "scheduled"}, {"status": "scheduled", "start_at": "2026-10-10T13:00:00Z", "end_at": "2026-10-10T12:00:00Z", "timezone": "UTC"}, {"status": "scheduled", "start_at": "2026-10-10T12:00:00", "end_at": "2026-10-10T13:00:00", "timezone": "UTC"}):
            with self.assertRaises(DomainError):
                self.run_command("schedule_interview", {"application_id": app["id"], **fields})
        with self.assertRaises(DomainError):
            self.run_command("record_browser_observation", {"application_id": app["id"], "device_id": "d", "observation_id": "o", "attempt_ref": "a", "activity": "page_view"})

    def test_same_message_multiple_requests_and_replay_suppression(self):
        app = self.app()
        base = {"application_id": app["id"], "kind": "reply", "description": "Reply", "evidence": [{"source_id": "mail-1"}]}
        first = self.run_command("record_request", {**base, "origin_ref": "finding-1"})
        second = self.run_command("record_request", {**base, "kind": "complete_assessment", "description": "Do assessment", "origin_ref": "finding-2"})
        self.assertNotEqual(first["id"], second["id"])
        self.run_command("cancel_task", {"task_id": first["id"], "expected_version": 1, "reason": "Not required"})
        replayed = self.run_command("record_request", {**base, "origin_ref": "finding-1"})
        self.assertEqual(replayed["status"], "cancelled")
        with self.executor.read() as con:
            affected = self.api.affected_by_evidence(con, app["id"], "mail-1")
            self.assertEqual(len(affected["items"]), 2)
            self.assertFalse(affected["truncated"])
            self.assertTrue(self.api.affected_by_evidence(con, app["id"], "mail-1", limit=1)["truncated"])

    def test_reminder_delivery_never_completes_task_and_snooze_fences_old_version(self):
        task = self.run_command("create_task", {"job_source": self.source, "kind": "reply", "description": "Reply", "due_at": "2026-10-10T12:00:00Z", "reminders_enabled": True})
        with self.executor.read() as con:
            reminder = self.api.list_records(con, task["application_id"], "reminders")["items"][0]
        self.run_command("snooze_task", {"task_id": task["id"], "expected_version": 1, "until": "2026-10-10T14:00:00Z"})
        self.clock = "2026-10-10T15:00:00Z"
        with self.assertRaises(DomainError):
            self.executor.run(CommandContext(self.principal, "old-reminder"), "mark_reminder_delivered", {}, lambda tx: self.api.mark_reminder_delivered(tx, reminder["id"], 1))
        self.executor.run(CommandContext(self.principal, "new-reminder"), "mark_reminder_delivered", {}, lambda tx: self.api.mark_reminder_delivered(tx, reminder["id"], 2))
        self.assertEqual(self.get("tasks", task["id"])["status"], "open")

    def test_correction_of_round_requires_explicit_task_resolution(self):
        app = self.app()
        interview = self.run_command("schedule_interview", {"application_id": app["id"], "create_task": True}, causation_id="p1")
        with self.executor.read() as con:
            task = self.api.list_records(con, app["id"], "tasks")["items"][0]
        correction = {"application_id": app["id"], "reason": "Wrong source", "corrections": [{"kind": "interviews", "id": interview["id"], "expected_version": 1, "resolution": "retract"}]}
        with self.assertRaises(DomainError):
            self.run_command("correct_progress", correction)
        correction["corrections"].append({"kind": "tasks", "id": task["id"], "expected_version": 1, "resolution": "keep"})
        self.run_command("correct_progress", correction)
        self.assertEqual(self.get("tasks", task["id"])["status"], "open")
        self.assertEqual(self.stage(app["id"]), "tracking")

    def test_correction_discovery_includes_later_independent_dependents(self):
        app = self.app()
        interview = self.run_command("schedule_interview", {"application_id": app["id"], "evidence": [{"source_id": "mail-1"}]}, causation_id="p1")
        task = self.run_command("create_task", {"application_id": app["id"], "kind": "attend_interview", "description": "My own preparation", "related_id": interview["id"]})
        with self.executor.read() as con:
            result = self.api.affected_by_evidence(con, app["id"], "mail-1")
        self.assertEqual({item["id"] for item in result["items"]}, {interview["id"], task["id"]})
        self.assertTrue(next(item for item in result["items"] if item["id"] == task["id"])["independently_edited"])

    def test_combination_preserves_distinct_attempts_aliases_and_pursuit_histories(self):
        target = self.app()
        source = self.run_command("save_job", {"job_source": {"source": "lever", "source_id": "other"}})
        for app in (target, source):
            self.run_command("record_submission", {"application_id": app["id"], "answers": {"why": "old"}})
            self.run_command("close_application", {"application_id": app["id"], "expected_version": 1, "reason": "Stopped"})
            self.run_command("reopen_application", {"application_id": app["id"], "expected_version": 2, "reason": "Again"})
            self.run_command("confirm_submission", {"application_id": app["id"], "answers": {"why": "current"}})
        with self.executor.read() as con:
            expected = self.api.preview_combination(con, source["id"], target["id"])["expected_records"]
        result = self.run_command("combine_jobs", {"source_application_id": source["id"], "target_application_id": target["id"], "source_version": 3, "target_version": 3, "disposition": "open", "reason": "Reviewed same requisition", "expected_records": expected})
        self.assertEqual(result["resolved_application_id"], target["id"])
        with self.executor.read() as con:
            self.assertEqual(self.api.get_application(con, source["id"])["id"], target["id"])
            records = self.api.list_records(con, target["id"], "submissions")["items"]
            self.assertEqual(len(records), 4)
            self.assertEqual(len(self.api.list_records(con, target["id"], "submissions", current_only=True)["items"]), 2)
            self.assertEqual(len({item["pursuit_no"] for item in records}), 3)
            self.assertEqual(len(self.api.get_job(con, target["job_id"])["sources"]), 2)
        note = self.run_command("add_note", {"job_source": {"source": "lever", "source_id": "other"}, "text": "Alias"})
        self.assertEqual(note["application_id"], target["id"])

    def test_migration_preserves_exact_evidence_and_queues_nothing(self):
        worker = Principal("converter", "worker", frozenset({"import_snapshot"}))
        def migrate(tx):
            app = self.api.import_application(tx, {"id": "old-app", "job_id": "old-job", "job": {"sources": [{"source": "lever", "source_id": "old-posting"}]}, "created_at": "2020-01-01T00:00:00Z"})
            attempt = self.api.import_record(tx, "submissions", {"id": "old-attempt", "application_id": app["id"], "status": "confirmed", "answers": {"exact": "e\u0301\n"}, "documents": [{"hash": "historic"}], "evidence": [], "created_at": "2020-01-01T00:00:00Z"}, {"source": "snapshot"})
            self.api.import_record(tx, "reminders", {"id": "old-reminder", "application_id": app["id"], "status": "delivered", "related_id": None, "at": "2020-01-01T00:00:00Z", "next_notification_at": "2020-01-01T00:00:00Z", "kind": "standalone"})
            return attempt
        with self.assertRaises(DomainError):
            self.executor.run(CommandContext(self.principal, "bad-import"), "import_snapshot", {}, migrate)
        result = self.executor.run(CommandContext(worker, "import", "migration"), "import_snapshot", {}, migrate)
        self.assertEqual(result["answers"]["exact"], "e\u0301\n")
        self.assertEqual(self.stage("old-app"), "active")
        with self.executor.read() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM command_work").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM app_feedback").fetchone()[0], 1)

    def test_global_queries_filter_before_pagination_and_compare_real_instants(self):
        app = self.app()
        first = self.run_command("create_task", {"application_id": app["id"], "kind": "reply", "description": "Done"})
        second = self.run_command("create_task", {"application_id": app["id"], "kind": "reply", "description": "Open"})
        self.run_command("complete_task", {"task_id": first["id"], "expected_version": 1, "reason": "Done"})
        interview = self.run_command("schedule_interview", {"application_id": app["id"], "status": "scheduled", "start_at": "2026-10-12T09:00:00-05:00", "end_at": "2026-10-12T10:00:00-05:00", "timezone": "America/Chicago"})
        with self.executor.read() as con:
            page = self.api.query_records(con, "tasks", statuses=["open"], limit=1)
            self.assertEqual([row["id"] for row in page["items"]], [second["id"]])
            self.assertFalse(page["truncated"])
            page = self.api.query_records(con, "interviews", starts_after="2026-10-12T13:30:00Z", starts_before="2026-10-12T14:30:00Z")
            self.assertEqual([row["id"] for row in page["items"]], [interview["id"]])


if __name__ == "__main__":
    unittest.main()
