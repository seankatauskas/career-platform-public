"""Shared views preserve owner facts, coverage, cursor scope and read purity."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from job_search.application_compatibility import translate_tool
from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter, AgentApplicationAdapter
from job_search.commands import CommandContext, DomainError, Principal


class ViewsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ApplicationRuntime(Path(self.tmp.name) / "candidate.db", clock=lambda: "2026-10-09T12:00:00Z")
        self.human = HumanApplicationAdapter(self.runtime, "human")
        self.agent = AgentApplicationAdapter(self.runtime)
        self.serial = 0
        self.app = self.command("save_job", {"job_source": {"source": "fixture", "source_id": "role", "employer": "Example", "title": "Engineer"}})

    def command(self, operation, payload):
        self.serial += 1
        return self.human.command(operation, payload, str(self.serial))

    def task(self, description="Reply"):
        return self.command("create_task", {"application_id": self.app["id"], "kind": "reply", "description": description})

    def query_tool(self, tool, payload):
        translated = translate_tool(tool, payload)
        self.assertEqual(translated.kind, "query")
        return self.runtime.queries.query(translated.operation, dict(translated.input))

    def receipt_counts(self):
        with self.runtime.executor.read() as con:
            return tuple(con.execute("SELECT count(*) FROM " + table).fetchone()[0]
                         for table in ("command_receipts", "command_history", "command_work"))

    def test_workspace_briefing_agent_agree_and_report_failed_evidence_without_mutation(self):
        task = self.task()
        worker = Principal("analysis", "worker", {"record_analysis"})
        self.runtime.executor.run(CommandContext(worker, "failure", "inferred"), "record_analysis", {},
            lambda tx: self.runtime.understanding.record_source_failure(tx,
                source_refs=[{"source_id": "message", "revision": "r1", "sha256": "a" * 64}],
                failure_code="source_unavailable", candidate_ids=[self.app["id"]]))
        counts = self.receipt_counts()
        workspace = self.runtime.queries.workspace(self.app["id"])
        briefing = self.runtime.queries.briefing()
        agent = self.agent.call("get_application_workspace", {"application_id": self.app["id"]})
        self.assertEqual(workspace["job"]["employer"], "Example")
        self.assertEqual(briefing["applications"]["items"][0]["job"]["title"], "Engineer")
        self.assertEqual(workspace["records"]["tasks"]["items"][0]["id"], task["id"])
        self.assertEqual(agent["progress"], workspace["progress"])
        self.assertEqual(briefing["analysis_coverage"]["items"], workspace["analysis_coverage"]["items"])
        self.assertEqual(workspace["analysis_coverage"]["items"][0]["failure_code"], "source_unavailable")
        self.assertEqual(counts, self.receipt_counts())

    def test_briefing_uses_exactly_one_read_snapshot(self):
        reads = []
        original = self.runtime.executor.read
        @contextmanager
        def read():
            reads.append(True)
            with original() as con:
                yield con
        self.runtime.executor.read = read
        self.runtime.queries.briefing()
        self.assertEqual(len(reads), 1)

    def test_task_filter_precedes_limit_and_cursors_reject_changed_scope(self):
        tasks = [self.task(str(i)) for i in range(4)]
        self.command("complete_task", {"task_id": tasks[0]["id"], "expected_version": 1, "reason": "Done"})
        completed = self.query_tool("list_application_tasks", {"application_id": self.app["id"], "status": "completed", "limit": 1})
        self.assertEqual([t["id"] for t in completed["items"]], [tasks[0]["id"]])
        page = self.query_tool("list_application_tasks", {"application_id": self.app["id"], "status": "open", "limit": 1})
        second = self.query_tool("list_application_tasks", {"application_id": self.app["id"], "status": "open", "limit": 1, "cursor": page["next_cursor"]})
        self.assertNotEqual(page["items"][0]["id"], second["items"][0]["id"])
        with self.assertRaises(DomainError):
            self.query_tool("list_application_tasks", {"application_id": self.app["id"], "status": "completed", "cursor": page["next_cursor"]})
        with self.assertRaises(DomainError):
            self.runtime.queries.query("tasks", {"application_id": self.app["id"], "ignored_filter": "bad"})

    def test_mixed_detail_pagination_and_history_preserve_all_rows(self):
        expected = set()
        for i in range(2):
            expected.add(self.command("record_assessment", {"application_id": self.app["id"], "description": "Assessment " + str(i)})["id"])
        expected.add(self.command("record_offer", {"application_id": self.app["id"], "terms": {"title": "Engineer"}})["id"])
        actual, cursor = [], None
        while True:
            payload = {"application_id": self.app["id"], "limit": 1}
            if cursor:
                payload["cursor"] = cursor
            page = self.query_tool("list_application_details", payload)
            actual.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(expected))
        task = self.task()
        self.command("complete_task", {"task_id": task["id"], "expected_version": 1, "reason": "Done"})
        history = self.query_tool("get_application_record_history", {"kind": "task", "record_id": task["id"], "limit": 1})
        self.assertEqual(history["items"][0]["operation"], "create_task")
        next_page = self.query_tool("get_application_record_history", {"kind": "task", "record_id": task["id"], "limit": 1, "cursor": history["next_cursor"]})
        self.assertEqual(next_page["items"][0]["operation"], "complete_task")
        with self.assertRaises(DomainError):
            self.query_tool("get_application_record_history", {"kind": "task", "record_id": task["id"], "after_revision": 1})

    def test_interview_time_filters_and_global_reminders_use_public_owner_queries(self):
        interview = self.command("schedule_interview", {"application_id": self.app["id"], "status": "scheduled",
            "start_at": "2026-10-11T15:00:00Z", "end_at": "2026-10-11T16:00:00Z", "timezone": "America/Chicago"})
        page = self.query_tool("list_interview_rounds", {"statuses": ["confirmed"], "starts_after": "2026-10-10T00:00:00Z", "starts_before": "2026-10-12T00:00:00Z"})
        self.assertEqual(page["items"][0]["id"], interview["id"])
        empty = self.query_tool("list_interview_rounds", {"starts_before": "2026-10-10T00:00:00Z"})
        self.assertEqual(empty["items"], [])
        payload = {"application_id": self.app["id"], "at": "2026-10-10T00:00:00Z", "description": "Follow up"}
        reminder = self.runtime.executor.run(CommandContext(self.human.principal, "reminder"), "create_reminder", payload,
            lambda tx: self.runtime.applications.create_reminder(tx, payload))
        result = self.query_tool("list_reminders", {"status": "scheduled"})
        self.assertIn(reminder["id"], [r["id"] for r in result["items"]])

    def test_mail_search_requires_an_authorized_bounded_source(self):
        with self.assertRaises(DomainError) as caught:
            self.query_tool("search_mail_history", {"query": "assessment"})
        self.assertEqual(caught.exception.code, "source_unavailable")
        class Source:
            def search_mail_page(self, query, limit, cursor=None):
                return {"items": [{"query": query}], "next_cursor": None, "coverage": {"complete": True}}
        self.runtime.queries.mail_source = Source()
        self.assertEqual(self.query_tool("search_mail_history", {"query": "assessment"})["items"], [{"query": "assessment"}])
        with self.assertRaises(DomainError):
            self.query_tool("search_mail_history", {"query": "assessment", "limit": 100})


if __name__ == "__main__":
    unittest.main()
