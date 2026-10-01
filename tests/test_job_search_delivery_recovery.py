#!/usr/bin/env python3
"""Offline crash/restart contract tests for the Hermes delivery receipt journal."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from job_search.hermes_delivery import (DeliveryReceiptStore, HermesDeliveryClient,
    HermesDeliveryBridgeError, HermesDispatcher, _HermesServer)
from job_search.notifications import RemoteHermesSendClient, NotificationSendError
from job_search.notifications import DurableNotificationPublisher, NotificationIntent, NotificationOutboxHandler
from job_search.delivery_recovery import NotificationRecoveryService
from job_search.contracts import MutationContext, ContractError
from job_search.service import JobSearchLedger
from job_search.store import ConflictError


PAYLOAD = {"delivery_id": "notice-1", "title": "Interview", "body": "Review the proposed slots"}


def dispatcher_at(root):
    executable = root / "hermes"
    executable.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n")
    executable.chmod(0o700)
    return HermesDispatcher(executable, target="telegram:owner", receipts=DeliveryReceiptStore(root / "receipts.sqlite"))


def expect_code(code, operation):
    try:
        operation()
    except HermesDeliveryBridgeError as exc:
        assert exc.code == code and not exc.retryable
        return
    raise AssertionError("expected " + code)


def test_receipt_replay_survives_restart_and_rejects_changed_payload():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sender = dispatcher_at(root)
        assert sender.dispatch("send", PAYLOAD) == {"delivered": True}
        restarted = dispatcher_at(root)
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", side_effect=AssertionError("replayed send")):
            assert restarted.dispatch("send", PAYLOAD) == {"delivered": True}
            expect_code("delivery_payload_conflict", lambda: restarted.dispatch("send", {**PAYLOAD, "body": "different"}))
            restarted.target = "telegram:other"
            expect_code("delivery_payload_conflict", lambda: restarted.dispatch("send", PAYLOAD))
        assert restarted.receipts.status("notice-1")["attempts"] == 1


def test_crash_after_launch_requires_exact_reconciliation_before_retry():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sender = dispatcher_at(root)
        process = mock.Mock()
        process.communicate.side_effect = SystemExit("simulated crash")
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", return_value=process):
            try:
                sender.dispatch("send", PAYLOAD)
            except SystemExit:
                pass
        restarted = dispatcher_at(root)
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", side_effect=AssertionError("blind resend")):
            expect_code("delivery_reconciliation_required", lambda: restarted.dispatch("send", PAYLOAD))
        state = restarted.receipts.status("notice-1")
        assert state["state"] == "reconciliation_required" and state["attempts"] == 1
        request = {"delivery_id": "notice-1", "expected_attempts": 1,
            "expected_payload_sha256": state["payload_sha256"], "outcome": "not_delivered"}
        assert restarted.dispatch("reconcile", request)["state"] == "retryable"
        assert restarted.dispatch("reconcile", request)["state"] == "retryable"
        restarted.dispatch("send", PAYLOAD)
        assert restarted.receipts.status("notice-1")["attempts"] == 2
        expect_code("request_rejected", lambda: restarted.dispatch("reconcile", request))


def test_crash_before_receipt_commit_does_not_repeat_completed_external_send():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sender = dispatcher_at(root)
        with mock.patch.object(sender.receipts, "finish", side_effect=sqlite3.OperationalError("disk failure")):
            expect_code("delivery_reconciliation_required", lambda: sender.dispatch("send", PAYLOAD))
        restarted = dispatcher_at(root)
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", side_effect=AssertionError("blind resend")):
            expect_code("delivery_reconciliation_required", lambda: restarted.dispatch("send", PAYLOAD))
            state = restarted.receipts.status("notice-1")
            request = {"delivery_id": "notice-1", "expected_attempts": 1,
                "expected_payload_sha256": state["payload_sha256"], "outcome": "delivered"}
            assert restarted.dispatch("reconcile", request)["state"] == "delivered"
            assert restarted.dispatch("send", PAYLOAD) == {"delivered": True}


def test_prelaunch_failure_is_retryable_but_timeout_is_not():
    with tempfile.TemporaryDirectory() as directory:
        sender = dispatcher_at(Path(directory))
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", side_effect=OSError("missing executable")):
            try:
                sender.dispatch("send", PAYLOAD)
            except HermesDeliveryBridgeError as exc:
                assert exc.retryable
            else:
                raise AssertionError("expected prelaunch failure")
        assert sender.receipts.status("notice-1")["state"] == "retryable"
        process = mock.Mock(pid=99999999)
        process.communicate.side_effect = [subprocess.TimeoutExpired("hermes", 30), (None, None)]
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", return_value=process), mock.patch("job_search.hermes_delivery.os.killpg"):
            expect_code("delivery_reconciliation_required", lambda: sender.dispatch("send", PAYLOAD))
        assert sender.receipts.status("notice-1")["state"] == "reconciliation_required"
        assert sender.receipts.status("notice-1")["attempts"] == 2


def test_old_receipts_migrate_without_repeating_a_known_delivery():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "receipts.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE delivered(delivery_id TEXT PRIMARY KEY,delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            connection.execute("INSERT INTO delivered(delivery_id) VALUES ('notice-1')")
        database.chmod(0o600)
        sender = dispatcher_at(root)
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", side_effect=AssertionError("legacy resend")):
            assert sender.dispatch("send", PAYLOAD) == {"delivered": True}
        assert sender.receipts.status("notice-1")["state"] == "legacy_delivered"


def test_socket_preserves_reconciliation_code_and_allows_only_exact_decision():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sender = dispatcher_at(root)
        process = mock.Mock(returncode=1)
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", return_value=process):
            expect_code("delivery_reconciliation_required", lambda: sender.dispatch("send", PAYLOAD))
        socket_path = root / "delivery.sock"
        server = _HermesServer(socket_path, sender)
        socket_path.chmod(0o600)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            remote = RemoteHermesSendClient(socket_path, target="telegram:owner")
            try:
                remote.send({"notification_id": "notice-1", "title": PAYLOAD["title"], "body": PAYLOAD["body"]})
            except NotificationSendError as exc:
                assert str(exc) == "delivery_reconciliation_required" and not exc.retryable
            else:
                raise AssertionError("expected unknown outcome")
            client = HermesDeliveryClient(socket_path, expected_target="telegram:owner")
            state = client.status("notice-1")
            expect_code("request_rejected", lambda: client.reconcile("notice-1", expected_attempts=2,
                expected_payload_sha256=state["payload_sha256"], outcome="delivered"))
            assert client.reconcile("notice-1", expected_attempts=1,
                expected_payload_sha256=state["payload_sha256"], outcome="abandoned")["state"] == "abandoned"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@contextmanager
def uncertain_notification(root):
    dispatcher = dispatcher_at(root)
    path = root / "recovery.sock"
    server = _HermesServer(path, dispatcher)
    path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        ledger = JobSearchLedger(root / "ledger.sqlite")
        notification = DurableNotificationPublisher(ledger).publish(NotificationIntent(
            "reminder.due", "reminder-1", "Review application", "Private reminder body"))["notification"]
        remote = RemoteHermesSendClient(path, target="telegram:owner")
        process = mock.Mock(returncode=1)
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", return_value=process):
            assert NotificationOutboxHandler(ledger, remote).handle_task({"limit": 1})["dead"] == 1
        bridge = HermesDeliveryClient(path, expected_target="telegram:owner")
        service = NotificationRecoveryService(ledger, bridge)
        yield ledger, dispatcher, remote, service, notification
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_application_reconciliation_requeues_exact_payload_without_sending():
    with tempfile.TemporaryDirectory() as directory, uncertain_notification(Path(directory)) as (ledger, dispatcher, remote, service, notification):
        listing = service.list_pending()
        item = listing["items"][0]
        assert listing["bridge_available"] and "body" not in str(listing) and "title" not in item
        request = {"expected_attempts": item["expected_attempts"],
            "expected_payload_sha256": item["expected_payload_sha256"], "outcome": "not_delivered",
            "context": MutationContext("reconcile-one", "user", "dashboard")}
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", side_effect=AssertionError("handler must not send")):
            result = service.reconcile(notification["notification_id"], **request)
            assert result["status"] == "pending" and result["attempts"] == 0
            assert service.reconcile(notification["notification_id"], **request) == result
        pending = ledger.list_notification_outbox(("pending",))[0]
        assert pending["title"] == notification["title"] and pending["body"] == notification["body"]
        assert NotificationOutboxHandler(ledger, remote).handle_task({"limit": 1})["delivered"] == 1
        assert dispatcher.receipts.status(notification["notification_id"])["attempts"] == 2
        with sqlite3.connect(ledger.store.db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM command_results WHERE command_name LIKE '%notification%reconciliation%' OR command_name='reconcile_notification_delivery'").fetchone()[0] == 2


def test_application_reconciliation_rejects_stale_or_nonuser_and_can_abandon():
    with tempfile.TemporaryDirectory() as directory, uncertain_notification(Path(directory)) as (ledger, dispatcher, remote, service, notification):
        item = service.list_pending()["items"][0]
        request = {"expected_attempts": item["expected_attempts"],
            "expected_payload_sha256": item["expected_payload_sha256"], "outcome": "abandoned",
            "context": MutationContext("abandon-one", "user", "dashboard")}
        for changes, expected_error in (({"expected_attempts": 99}, ConflictError),
            ({"expected_payload_sha256": "f" * 64}, ConflictError),
            ({"outcome": {}}, ContractError),
            ({"context": MutationContext("agent-request", "hermes", "test")}, ContractError)):
            try:
                service.reconcile(notification["notification_id"], **{**request, **changes})
            except expected_error:
                pass
            else:
                raise AssertionError("invalid reconciliation was accepted")
        assert service.reconcile(notification["notification_id"], **request)["status"] == "cancelled"
        assert NotificationOutboxHandler(ledger, remote).handle_task({"limit": 1})["delivered"] == 0


def test_application_reconciliation_recovers_after_bridge_commit_before_app_commit():
    with tempfile.TemporaryDirectory() as directory, uncertain_notification(Path(directory)) as (ledger, dispatcher, remote, service, notification):
        item = service.list_pending()["items"][0]
        request = {"expected_attempts": item["expected_attempts"],
            "expected_payload_sha256": item["expected_payload_sha256"], "outcome": "delivered",
            "context": MutationContext("delivered-one", "user", "dashboard")}
        original = service.bridge.reconcile
        def crash_after_bridge(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated process death after bridge commit")
        with mock.patch.object(service.bridge, "reconcile", side_effect=crash_after_bridge):
            try:
                service.reconcile(notification["notification_id"], **request)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected crash")
        assert ledger.list_notification_outbox(("dead",))[0]["last_error"] == "delivery_reconciliation_required"
        assert service.list_pending()["items"][0]["bridge_state"] == "delivered"
        with mock.patch("job_search.hermes_delivery.subprocess.Popen", side_effect=AssertionError("must not resend")):
            result = service.reconcile(notification["notification_id"], **request)
            assert result["status"] == "delivered"
            assert service.reconcile(notification["notification_id"], **request) == result
        assert service.list_pending()["items"] == []


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} delivery recovery tests)")
