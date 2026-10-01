#!/usr/bin/env python3
"""Offline checks for the private cloud Hermes delivery bridge."""

from __future__ import annotations

import os
import pwd
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from job_search.hermes_delivery import (
    SERVICE_REVISION,
    DeliveryReceiptStore,
    HermesDeliveryBridgeError,
    HermesDeliveryClient,
    HermesDispatcher,
    _HermesServer,
    _safe_socket,
    _target_fingerprint,
    install_mcp_environment,
)
from job_search.notifications import RemoteHermesSendClient
from job_search.runtime import RuntimeConfigV1, build_runtime


def _private_directory(root: Path) -> Path:
    target = root / "socket"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    return target


def test_client_protocol_and_remote_notification_adapter() -> None:
    class Dispatcher:
        def __init__(self) -> None:
            self.calls = []

        def dispatch(self, operation, payload):
            self.calls.append((operation, dict(payload)))
            if operation == "ping":
                return {
                    "service_revision": SERVICE_REVISION,
                    "gateway_running": True,
                    "target_fingerprint": _target_fingerprint("telegram:12345"),
                }
            return {"delivered": True}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        socket_path = _private_directory(root) / "hermes.sock"
        dispatcher = Dispatcher()
        server = _HermesServer(socket_path, dispatcher)
        os.chmod(socket_path, 0o600)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
        )
        thread.start()
        try:
            client = HermesDeliveryClient(socket_path)
            assert client.ping()["gateway_running"] is True
            sender = RemoteHermesSendClient(socket_path, target="telegram:12345")
            sender.send(
                {
                    "notification_id": "notification_123",
                    "title": "Interview",
                    "body": "Tuesday at 10",
                }
            )
            assert dispatcher.calls[-1] == (
                "send",
                {
                    "delivery_id": "notification_123",
                    "title": "Interview",
                    "body": "Tuesday at 10",
                },
            )
        finally:
            server.shutdown()
            server.server_close()
            socket_path.unlink(missing_ok=True)
            thread.join(timeout=2)
        assert not thread.is_alive()


def test_dispatcher_invokes_only_fixed_hermes_send_argv() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        executable = root / "hermes"
        captured = root / "captured"
        executable.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$HERMES_TEST_CAPTURE"\ncat >> "$HERMES_TEST_CAPTURE"\n',
            encoding="utf-8",
        )
        os.chmod(executable, 0o700)
        previous = os.environ.get("HERMES_TEST_CAPTURE")
        os.environ["HERMES_TEST_CAPTURE"] = str(captured)
        try:
            receipts = DeliveryReceiptStore(root / "receipts.sqlite")
            dispatcher = HermesDispatcher(
                executable,
                target="ntfy:job-search",
                receipts=receipts,
            )
            result = dispatcher.dispatch(
                "send",
                {
                    "delivery_id": "notification_456",
                    "title": "New shortlist",
                    "body": "Three jobs",
                },
            )
            replay = dispatcher.dispatch(
                "send",
                {
                    "delivery_id": "notification_456",
                    "title": "New shortlist",
                    "body": "Three jobs",
                },
            )
        finally:
            if previous is None:
                os.environ.pop("HERMES_TEST_CAPTURE", None)
            else:
                os.environ["HERMES_TEST_CAPTURE"] = previous
        assert result == {"delivered": True}
        assert replay == {"delivered": True}
        assert captured.read_text(encoding="utf-8") == (
            "send\n--to\nntfy:job-search\nNew shortlist\n\nThree jobs"
        )


def test_socket_and_payload_boundaries_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        unsafe = root / "unsafe"
        unsafe.mkdir(mode=0o755)
        os.chmod(unsafe, 0o755)
        try:
            _safe_socket(unsafe / "hermes.sock", require_exists=False)
        except HermesDeliveryBridgeError as exc:
            assert not exc.retryable
        else:
            raise AssertionError(
                "world-readable delivery socket directory was accepted"
            )

        private = _private_directory(root)
        client = HermesDeliveryClient(
            private / "missing.sock", expected_target="telegram"
        )
        try:
            client.send("invalid id", "title", "body")
        except HermesDeliveryBridgeError as exc:
            assert not exc.retryable
        else:
            raise AssertionError("unsafe Hermes target was accepted")


def test_runtime_accepts_exactly_one_notification_transport() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        socket_path = _private_directory(root) / "hermes.sock"
        config = RuntimeConfigV1.from_mapping(
            {
                "version": 1,
                "project_root": str(root),
                "hermes_notification_socket": str(socket_path),
                "hermes_telegram_target": "telegram",
            }
        )
        runtime = build_runtime(config, base_environment={})
        handler = runtime.worker.task_handlers["notification.deliver"]
        assert isinstance(handler.__self__._sender, RemoteHermesSendClient)

        executable = root / "hermes"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(executable, 0o700)
        try:
            RuntimeConfigV1.from_mapping(
                {
                    "version": 1,
                    "project_root": str(root),
                    "hermes_executable": str(executable),
                    "hermes_notification_socket": str(socket_path),
                }
            )
        except ValueError as exc:
            assert "mutually exclusive" in str(exc)
        else:
            raise AssertionError("two Hermes notification transports were accepted")


def test_mcp_token_is_installed_once_into_private_runtime_environment() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        token = root / "mcp-token"
        token.write_text("a" * 48 + "\n", encoding="ascii")
        os.chmod(token, 0o600)
        environment = root / "environment"
        environment.mkdir(mode=0o700)
        os.chmod(environment, 0o700)
        owner_name = pwd.getpwuid(os.geteuid()).pw_name

        install_mcp_environment(token, environment, owner_name=owner_name)
        installed = environment / "JOB_SEARCH_MCP_TOKEN"
        assert installed.read_text(encoding="ascii") == "a" * 48
        assert installed.stat().st_mode & 0o777 == 0o600

        try:
            install_mcp_environment(token, environment, owner_name=owner_name)
        except HermesDeliveryBridgeError as exc:
            assert not exc.retryable and "already exists" in str(exc)
        else:
            raise AssertionError("existing s6 environment token was overwritten")


def test_s6_gateway_probe_is_bounded_and_reported_by_ping() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        executable = root / "hermes"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(executable, 0o700)
        dispatcher = HermesDispatcher(
            executable,
            target="telegram",
            receipts=DeliveryReceiptStore(root / "receipts.sqlite"),
            gateway_service=Path("/run/service/gateway-default"),
        )
        with (
            mock.patch("pathlib.Path.is_file", return_value=True),
            mock.patch("job_search.hermes_delivery.os.access", return_value=True),
            mock.patch(
                "job_search.hermes_delivery.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="true\n"),
            ) as run,
        ):
            report = dispatcher.dispatch("ping", {})
            assert report["gateway_running"] is True
            # A successful status query can report a stopped process. Empty,
            # malformed, and failed queries must also remain unhealthy.
            for code, output in ((0, "false\n"), (0, ""), (0, "true false\n"), (1, "true\n")):
                run.return_value = SimpleNamespace(returncode=code, stdout=output)
                assert dispatcher.dispatch("ping", {})["gateway_running"] is False
        assert report["gateway_running"] is True
        assert run.call_args.args[0] == (
            "/command/s6-svstat",
            "-u",
            "/run/service/gateway-default",
        )
        assert run.call_args.kwargs["timeout"] == 3


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} Hermes delivery tests)")


if __name__ == "__main__":
    main()
