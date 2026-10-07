#!/usr/bin/env python3
"""Offline checks for the production dashboard/Hermes composition root."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search.resume_lab.gateway import build_resume_lab_read_gateway
from job_search.runtime import RuntimeConfigV1
from job_search.system import (
    build_dashboard_controller,
    build_hermes_sources_from_config,
    initialize_mcp_token,
    read_mcp_token,
)


class MailSource:
    def search_mail(self, query, limit):
        return [{"message_id": "archive-1", "excerpt": query}][:limit]

    def get_mail_message(self, message_id):
        return {"message_id": message_id, "text": "sanitized"}


def test_mcp_token_is_private_valid_and_never_overwritten() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "private" / "mcp-token"
        created = initialize_mcp_token(path)
        assert created == path.resolve()
        assert os.stat(path).st_mode & 0o777 == 0o600
        token = read_mcp_token(path)
        assert len(token) >= 32 and not any(character.isspace() for character in token)
        try:
            initialize_mcp_token(path)
        except FileExistsError:
            pass
        else:
            raise AssertionError("MCP token was overwritten")
        os.chmod(path, 0o644)
        try:
            read_mcp_token(path)
        except ValueError as exc:
            assert "owner-only" in str(exc)
        else:
            raise AssertionError("public MCP token was accepted")


def test_production_composition_uses_configured_paths_and_narrow_sources() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = RuntimeConfigV1.defaults(root)
        controller = build_dashboard_controller(config)
        assert controller.ledger.store.db_path == config.application_db
        assert controller.settings.timezone == config.timezone
        sources = build_hermes_sources_from_config(
            config, mail_source=MailSource(), availability=None
        )
        assert sources.mail.search_mail("interview", 1)[0]["message_id"] == "archive-1"
        assert not hasattr(sources, "outlook")
        assert not hasattr(sources, "approve")


def test_hermes_resume_view_never_constructs_model_or_document_tools() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gateway = build_resume_lab_read_gateway(
            SimpleNamespace(
                resume_lab_db=root / "resume.db",
                resume_artifact_root=root / "artifacts",
                application_db=root / "applications.db",
                # These deliberately point nowhere: the read view must not open them.
                resume_model_config=root / "missing-model.json",
                tool_service_socket=root / "missing-tools.sock",
            )
        )
        assert gateway is not None
        assert gateway.model is None
        assert gateway.toolchain is None


def test_dashboard_archive_analysis_is_lazy_and_respects_mail_configuration() -> None:
    from dataclasses import replace
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        config = RuntimeConfigV1.defaults(Path(directory))
        classifier = object()
        with patch('job_search.runtime._configured_mail_models', return_value=(classifier, None, None, 'test-v1')) as models:
            disabled = build_dashboard_controller(config)
            assert disabled.review_classifier_factory is None
            enabled = build_dashboard_controller(replace(config, remote_mail_inference_enabled=True))
            assert callable(enabled.review_classifier_factory)
            models.assert_not_called()
            assert enabled.review_classifier_factory() == (classifier, 'test-v1')
            models.assert_called_once()


def test_dashboard_only_receives_cost_snapshot_path_not_billing_clients() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = RuntimeConfigV1.defaults(Path(directory))
        target = str(Path(directory) / "costs" / "snapshot.json")
        with patch.dict(os.environ, {"JOB_SEARCH_COST_SNAPSHOT": target}), \
                patch("job_search.system.DashboardController") as constructor:
            build_dashboard_controller(config)
        assert constructor.call_args.kwargs["cost_snapshot_path"] == Path(target)
        assert not (Path(directory) / "costs").exists()
        with patch.dict(os.environ, {}, clear=True), \
                patch("job_search.system.DashboardController") as constructor:
            build_dashboard_controller(config)
        assert constructor.call_args.kwargs["cost_snapshot_path"] is None


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search system tests)")


if __name__ == "__main__":
    main()
