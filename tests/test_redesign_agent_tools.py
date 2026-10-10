"""Candidate tool registry exercised through the existing authenticated MCP host."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest

from job_search.application_agent_tools import ApplicationAgentTools
from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter
from job_search.hermes import HermesValidationError
from job_search.hermes_mcp import make_mcp_server
from tests.test_job_search_hermes_runtime import mcp_request, TOKEN


class AgentToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ApplicationRuntime(Path(self.tmp.name) / "candidate.db", clock=lambda: "2026-10-09T12:00:00Z")
        self.human = HumanApplicationAdapter(self.runtime, "human")
        self.app = self.human.command("save_job", {"job_source": {"source": "fixture", "source_id": "role", "employer": "Example", "title": "Engineer"}}, "save")
        self.tools = ApplicationAgentTools(self.runtime)

    def proposal(self, key="proposal", description="Exact e\u0301 request"):
        return {"application_id": self.app["id"], "kind": "task", "payload": {"values": {"kind": "reply", "description": description}}, "idempotency_key": key}

    @contextmanager
    def host(self):
        server = make_mcp_server(self.tools, TOKEN, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_registry_preserves_legacy_names_and_definitions_are_independent(self):
        definitions = self.tools.tool_definitions()
        names = tuple(d["name"] for d in definitions)
        self.assertEqual(names, self.tools.tool_names)
        for name in ("propose_application_update", "propose_interview_revision", "create_reminder", "cancel_reminder", "search_mail_history", "get_application_record_history"):
            self.assertIn(name, names)
        for name in ("authorize_action", "review_changes", "execute_action", "close_application", "complete_task", "import_snapshot"):
            self.assertNotIn(name, names)
        definitions[0]["input_schema"]["properties"]["limit"]["maximum"] = 10000
        self.assertEqual(self.tools.tool_definitions()[0]["input_schema"]["properties"]["limit"]["maximum"], 100)

    def test_replayed_mutations_only_create_one_pending_proposal(self):
        first = self.tools.invoke("propose_application_update", self.proposal())
        second = self.tools.invoke("propose_application_update", self.proposal())
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["input"]["description"], "Exact e\u0301 request")
        self.assertEqual(self.runtime.queries.workspace(self.app["id"])["records"]["tasks"]["items"], [])
        with self.assertRaises(HermesValidationError):
            self.tools.invoke("propose_application_update", self.proposal(description="Changed request"))
        self.assertEqual(len(self.runtime.queries.review_queue()["items"]), 1)

    def test_missing_keys_authority_injection_and_invalid_nested_shapes_fail_closed(self):
        payload = self.proposal()
        del payload["idempotency_key"]
        with self.assertRaises(HermesValidationError):
            self.tools.invoke("propose_application_update", payload)
        for field in ("principal", "actor_kind", "capabilities", "origin", "delegation", "auto_apply"):
            payload = self.proposal(key=field)
            payload["payload"]["values"][field] = "human"
            with self.assertRaises(HermesValidationError):
                self.tools.invoke("propose_application_update", payload)
        for name in ("authorize_action", "execute_action", "review_changes"):
            with self.assertRaises(HermesValidationError):
                self.tools.invoke(name, {})
        with self.assertRaises(HermesValidationError):
            self.tools.invoke("list_applications", {"limit": True})
        self.assertEqual(self.runtime.queries.review_queue()["items"], [])

    def test_reminder_creation_is_a_proposal_and_reads_use_compatibility_queries(self):
        reminder = self.tools.invoke("create_reminder", {"application_id": self.app["id"], "due_at": "2026-10-11T12:00:00Z", "note": "Follow up", "idempotency_key": "reminder"})
        self.assertEqual(reminder["operation"], "create_reminder")
        self.assertEqual(reminder["status"], "pending")
        self.assertEqual(self.tools.invoke("list_reminders", {"status": "scheduled"})["items"], [])
        self.assertEqual(self.tools.invoke("list_application_tasks", {"application_id": self.app["id"], "status": "open"})["items"], [])

    def test_real_host_lists_calls_rejects_forgery_and_preserves_exact_unicode(self):
        with self.host() as server:
            status, listed = mcp_request(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(status, 200)
            self.assertEqual([t["name"] for t in listed["result"]["tools"]], list(self.tools.tool_names))
            annotations = {tool["name"]: tool["annotations"] for tool in listed["result"]["tools"]}
            self.assertTrue(annotations["get_application_workspace"]["readOnlyHint"])
            self.assertTrue(annotations["propose_application_update"]["idempotentHint"])
            self.assertFalse(annotations["cancel_reminder"]["destructiveHint"])
            request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "propose_application_update", "arguments": self.proposal()}}
            status, response = mcp_request(server, request)
            self.assertEqual(status, 200)
            self.assertFalse(response["result"]["isError"])
            structured = response["result"]["structuredContent"]
            self.assertEqual(structured["input"]["description"], "Exact e\u0301 request")
            self.assertIn("e\u0301", response["result"]["content"][0]["text"])
            _, retry = mcp_request(server, request)
            self.assertEqual(retry["result"]["structuredContent"]["id"], structured["id"])
            _, rejected = mcp_request(server, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "authorize_action", "arguments": {}}})
            self.assertEqual(rejected["error"]["code"], -32602)
            request["params"]["arguments"]["principal"] = {"kind": "human"}
            _, forged = mcp_request(server, request)
            self.assertTrue(forged["result"]["isError"])
            status, _ = mcp_request(server, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, token="incorrect" * 8)
            self.assertEqual(status, 401)
        self.assertEqual(self.runtime.queries.workspace(self.app["id"])["records"]["tasks"]["items"], [])


if __name__ == "__main__":
    unittest.main()
