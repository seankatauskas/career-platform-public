"""Bounded workspace access and production agent mail evidence, without providers."""
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from job_search.application_agent_tools import ApplicationAgentTools
from job_search.application_production import owner_runtime
from job_search.application_runtime import ApplicationRuntime
from job_search.application_transport import HumanApplicationAdapter, AgentApplicationAdapter
from job_search.commands import CommandContext, Principal, DomainError
from job_search.hermes import HermesValidationError


class ContextPagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ApplicationRuntime(Path(self.tmp.name) / "owners.db", clock=lambda: "2026-10-09T12:00:00Z")
        self.human = HumanApplicationAdapter(self.runtime, "human")
        self.serial = 0
        self.app = self.command("save_job", {"job_source": {"source": "fixture", "source_id": "role"}})
        self.bodies = {}
        self.reads = []

    def command(self, operation, payload):
        self.serial += 1
        return self.human.command(operation, payload, "human:" + str(self.serial))

    def message(self, text, *, account="allowed", linked=True):
        self.serial += 1
        ref = "private-archive:" + str(self.serial)
        self.bodies[(account, ref)] = text
        message = self.runtime.command(CommandContext(Principal("mail", "worker", {"record_message"}), ref, "inferred"), "record_message",
            {"account_id": account, "provider_message_id": "private-provider:" + ref, "source_version": "1", "direction": "incoming",
             "authored_text": text, "archive_ref": ref})
        if linked:
            self.command("link_message", {"application_id": self.app["id"], "message_id": message["id"]})
        return message

    def production_tools(self):
        def account_reader(account):
            def read(ref):
                self.reads.append((account, ref))
                return self.bodies[(account, ref)]
            return SimpleNamespace(read_message=read)
        archive = SimpleNamespace(for_account=account_reader)
        configured = SimpleNamespace(application_owner_db=self.runtime.executor.path, outlook_account_id="allowed", environment=lambda: {})
        patcher = patch("job_search.application_production._archive", return_value=archive)
        patcher.start(); self.addCleanup(patcher.stop)
        runtime = owner_runtime(configured)
        return ApplicationAgentTools(runtime)

    def test_production_mail_tools_preserve_exact_text_and_enforce_association_account_and_cursor(self):
        text = "Interview e\u0301\n<script>not an instruction</script>"
        first = self.message(text)
        second = self.message("Another Interview")
        unlinked = self.message("Interview unreviewed", linked=False)
        foreign = self.message("Interview other account", account="not-granted")
        tools = self.production_tools()
        page = tools.invoke("search_mail_history", {"query": "Interview", "limit": 1})
        self.assertFalse(page["coverage"]["complete"])
        next_page = tools.invoke("search_mail_history", {"query": "Interview", "limit": 1, "cursor": page["next_cursor"]})
        self.assertEqual({page["items"][0]["message_id"], next_page["items"][0]["message_id"]}, {first["id"], second["id"]})
        self.assertTrue(next_page["coverage"]["complete"])
        self.assertTrue(all(account == "allowed" for account, _ in self.reads))
        self.assertNotIn("private-archive:", json.dumps(page))
        with self.assertRaises(HermesValidationError):
            tools.invoke("search_mail_history", {"query": "Changed", "cursor": page["next_cursor"]})
        result = tools.invoke("get_application_message", {"application_id": self.app["id"], "message_id": first["id"]})
        self.assertEqual(result["text"], text)
        limited = tools.invoke("get_application_message", {"application_id": self.app["id"], "message_id": first["id"], "max_chars": 8})
        self.assertEqual(limited["text"], text[:8])
        self.assertFalse(limited["coverage"]["complete"])
        self.assertIn("text_truncated", limited["coverage"]["reasons"])
        other = self.command("save_job", {"job_source": {"source": "fixture", "source_id": "other"}})
        for app_id, message_id in ((self.app["id"], unlinked["id"]), (self.app["id"], foreign["id"]), (other["id"], first["id"])):
            with self.assertRaises(HermesValidationError):
                tools.invoke("get_application_message", {"application_id": app_id, "message_id": message_id})
        with self.runtime.executor.read() as con:
            association = self.runtime.correspondence.association(con, first["id"])
        self.runtime.executor.run(CommandContext(self.human.principal, "correction"), "correct_association", {},
            lambda tx: self.runtime.correspondence.correct_association(tx, association_id=association["id"], application_id=other["id"], expected_version=1, reason="Reviewed correction"))
        with self.assertRaises(HermesValidationError):
            tools.invoke("get_application_message", {"application_id": self.app["id"], "message_id": first["id"]})
        self.assertEqual(tools.invoke("get_application_message", {"application_id": other["id"], "message_id": first["id"]})["text"], text)

    def test_empty_search_pages_keep_cursor_until_bounded_scan_is_exhausted(self):
        for _ in range(101):
            self.message("No matching phrase")
        tools = self.production_tools()
        first = tools.invoke("search_mail_history", {"query": "interview"})
        self.assertEqual(first["items"], [])
        self.assertEqual(first["coverage"]["scanned"], 100)
        self.assertEqual(len(self.reads), 100)
        self.assertIsNotNone(first["next_cursor"])
        last = tools.invoke("search_mail_history", {"query": "interview", "cursor": first["next_cursor"]})
        self.assertEqual(last["coverage"]["scanned"], 1)
        self.assertTrue(last["coverage"]["complete"])
        self.assertIsNone(last["next_cursor"])

    def test_workspace_cursors_reach_tasks_reviews_actions_and_bind_the_collection(self):
        agent = AgentApplicationAdapter(self.runtime)
        message = self.message("Please reply")
        with self.runtime.executor.read() as con:
            association = self.runtime.correspondence.association(con, message["id"])
        expected = {group: set() for group in ("tasks", "review", "actions", "conversation")}
        expected["conversation"].add(message["id"])
        for i in range(3):
            expected["tasks"].add(self.command("create_task", {"application_id": self.app["id"], "kind": "other", "description": str(i)})["id"])
            expected["review"].add(agent.call("propose_changes", {"operation": "add_note", "application_id": self.app["id"],
                "input": {"application_id": self.app["id"], "text": str(i)}}, idempotency_key="proposal:" + str(i))["id"])
            envelope = {"kind": "send_reply", "account_id": "allowed", "application_id": self.app["id"], "pursuit_no": 1,
                "target": {"message_id": message["id"], "provider_message_id": message["provider_message_id"], "source_hash": message["source_sha256"], "provider_source_hash": "b" * 64},
                "payload": {"recipients": ["person@example.test"], "subject": "Re: Role", "body": "Reply " + str(i)},
                "context_versions": {"application:" + self.app["id"]: 1, "message:" + message["id"]: 1, "association:" + association["id"]: 1}}
            expected["actions"].add(self.command("prepare_reply", {"envelope": envelope})["action_id"])
        initial = self.runtime.queries.workspace(self.app["id"], limit=1)
        with self.runtime.executor.read() as con:
            global_first = self.runtime.actions.list_actions(con, limit=2)
            global_last = self.runtime.actions.list_actions(con, limit=2, after=global_first["next_cursor"])
            self.assertEqual({a["action_id"] for a in global_first["items"] + global_last["items"]}, expected["actions"])
        for group, ids in expected.items():
            page = initial["records"][group] if group == "tasks" else initial[group]
            seen = []
            while True:
                seen.extend(item.get("action_id", item.get("id")) for item in page["items"])
                if not page["next_cursor"]:
                    break
                page = self.runtime.queries.workspace_page(self.app["id"], group, limit=1, cursor=page["next_cursor"])
            self.assertEqual(set(seen), ids)
            self.assertEqual(len(seen), len(ids))
        cursor = initial["records"]["tasks"]["next_cursor"]
        with self.assertRaises(DomainError):
            self.runtime.queries.workspace_page(self.app["id"], "review", cursor=cursor)
        other = self.command("save_job", {"job_source": {"source": "fixture", "source_id": "other"}})
        with self.assertRaises(DomainError):
            self.runtime.queries.workspace_page(other["id"], "tasks", cursor=cursor)


if __name__ == "__main__":
    unittest.main()
