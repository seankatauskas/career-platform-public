"""The owner installation governs reminders; retired attention cannot intervene."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch
import unittest

from job_search.activation import initialize_paused, set_control
from job_search.commands import CommandContext, Principal
from job_search.db import connect
from job_search.notifications import NotificationIntent
from job_search.runtime import build_runtime, NOTIFICATION_TASK
from job_search.system import build_dashboard_controller, build_hermes_sources_from_config, make_interaction_host
from tests import test_redesign_execution as execution_fixtures
from tests import test_redesign_production as production_fixtures


class OwnerAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = production_fixtures.ProductionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        self.config = replace(self.fixture.config,
            hermes_notification_socket=self.fixture.root / "fictional-notifications.sock",
            hermes_telegram_target="telegram")

    def core(self):
        return build_runtime(self.config, lane="core", base_environment={},
            now_provider=lambda: self.now, max_work_per_tick=50)

    def test_paused_legacy_notifications_cannot_suppress_activated_owner_reminder(self):
        f = self.fixture
        initialize_paused(self.config.application_db)
        f.runtime.executor.clock = lambda: self.now.isoformat().replace("+00:00", "Z")
        reminder = f.runtime.command(CommandContext(Principal("human", "human", {"*"}), "reminder"),
            "create_reminder", {"application_id": f.old,
                "at": "2026-10-09T12:01:00Z", "description": "Follow up"})
        f.runtime.executor.set_activation(paused=False, expected_revision=0,
            operator="fictional", reason="Authorize owner dispatch")
        client = execution_fixtures.FakeNotificationClient()
        with patch("job_search.hermes_delivery.HermesDeliveryClient", return_value=client):
            runtime = self.core()
        self.now = datetime(2026, 10, 9, 12, 2, tzinfo=timezone.utc)
        runtime.worker.tick()
        self.assertEqual(client.sends, 1)
        with f.runtime.executor.read() as con:
            record = f.runtime.applications.get_record(con, "reminders", reminder["id"])
            self.assertEqual(record["status"], "delivered")
        with closing(connect(self.config.application_db)) as con:
            self.assertEqual(con.execute("SELECT enabled FROM automation_controls WHERE capability='notifications'").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT enabled FROM schedule_specs WHERE task_kind='notification.deliver'").fetchone()[0], 0)

    def test_operational_notifications_bypass_attention_and_controls_preserve_pending_work(self):
        runtime = self.core()
        publisher = runtime.worker.task_handlers[NOTIFICATION_TASK].publisher
        with patch("job_search.attention.AttentionService.from_notification", side_effect=AssertionError("Retired attention route")):
            notification = publisher.publish(NotificationIntent(
                "shortlist.ready", "fictional-shortlist", "Shortlist ready", "Review the new shortlist"))["notification"]
        with patch("job_search.attention.AttentionService.on_activation_changed", side_effect=AssertionError("Retired attention activation")):
            set_control(self.config, "notifications", False, expected_revision=0, command_id="disable-operational")
            set_control(self.config, "notifications", True, expected_revision=1, command_id="enable-operational")
        with closing(connect(self.config.application_db)) as con:
            self.assertEqual(con.execute("SELECT status FROM notification_outbox WHERE notification_id=?",
                (notification["notification_id"],)).fetchone()[0], "pending")
            self.assertEqual(con.execute("SELECT count(*) FROM attention_preference_history").fetchone()[0], 0)

    def test_owner_hosts_and_workers_do_not_construct_retired_services_or_models(self):
        with patch("job_search.chief_runtime.configure_services") as configure, \
             patch("job_search.chief_runtime.build_generation_provider") as generation, \
             patch("job_search.interactions.notifications.InteractionNotificationSender") as sender:
            core = self.core()
            model = build_runtime(self.config, lane="model", base_environment={}, now_provider=lambda: self.now)
            build_dashboard_controller(self.config)
            build_hermes_sources_from_config(self.config)
            identity_config = replace(self.config, interaction_token_file=self.fixture.root / "fictional-bearer",
                telegram_bot_id="1", telegram_user_id="2", telegram_chat_id="3")
            with patch("job_search.system.read_mcp_token", return_value="fictional-token"), \
                 patch("job_search.interactions.server.make_interaction_server") as server:
                make_interaction_host(identity_config)
                retired = server.call_args.args[0]
                self.assertEqual(retired.identity, {"bot_id": "1", "user_id": "2", "chat_id": "3"})
            configure.assert_not_called()
            generation.assert_not_called()
            sender.assert_not_called()
            self.assertIn("application.execute_action", core.worker.task_handlers)
            self.assertIn("applications.mail.understand", model.worker.task_handlers)


if __name__ == "__main__":
    unittest.main()
