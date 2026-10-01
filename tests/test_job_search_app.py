#!/usr/bin/env python3
"""Offline checks for the job-search operator CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from job_search import cli as job_search_app


def invoke(arguments, *, environment=None):
    output = io.StringIO()
    with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output), patch.dict(
        os.environ, environment or {}, clear=True
    ), patch.object(job_search_app, "DEFAULT_CONFIG_PATH", Path(directory) / "not-configured.json"):
        status = job_search_app.main(arguments)
    return status, json.loads(output.getvalue())


def test_outlook_auth_parser_exposes_explicit_headless_device_code_mode():
    args = job_search_app.build_parser().parse_args(
        ["outlook-auth", "--device-code", "--enable-drafts"]
    )
    assert args.device_code is True and args.enable_drafts is True


def test_init_status_verify_and_dry_rebuild_are_offline_and_safe():
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "private" / "job-search.db"
        status, initialized = invoke(["--db", str(db_path), "init"])
        assert status == 0 and initialized["schema"] == "ready"
        assert initialized["schedules"]["ats_enabled"] is False
        assert initialized["schedules"]["outlook_enabled"] is False
        assert db_path.stat().st_mode & 0o777 == 0o600

        status, health = invoke(["--db", str(db_path), "status"])
        assert status == 0 and health["status"] == "healthy"
        assert health["dependencies"]["status"] == "ready"
        assert health["dependencies"]["inference"]["status"] == "disabled"
        assert health["dependencies"]["inference"]["remote_mail"] == {
            "egress_enabled": False,
            "active": False,
            "status": "disabled",
            "external_endpoint_probed": False,
        }
        assert health["dependencies"]["resume_lab"]["status"] == "disabled"
        status, verification = invoke(["--db", str(db_path), "verify"])
        assert status == 0 and verification == {
            "ok": True,
            "projection_failures": [],
        }
        status, preview = invoke(["--db", str(db_path), "rebuild"])
        assert status == 0 and preview == {
            "applied": False,
            "projection_failures": [],
        }


def test_mcp_token_init_creates_private_file_without_disclosing_value():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "project_root": str(root),
                    "mcp_token_file": "private/mcp-token",
                }
            ),
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        status, result = invoke(["--config", str(config_path), "mcp-token-init"])
        token_path = root / "private" / "mcp-token"
        assert status == 0
        assert result == {
            "created": True,
            "mcp_token_file": str(token_path.resolve()),
        }
        token = token_path.read_text(encoding="ascii").strip()
        assert len(token) >= 32 and token not in json.dumps(result)
        assert token_path.stat().st_mode & 0o777 == 0o600

        try:
            invoke(["--config", str(config_path), "mcp-token-init"])
        except SystemExit as exc:
            assert "refusing to overwrite" in str(exc)
        else:
            raise AssertionError("existing MCP token was overwritten")


def test_portable_encryption_key_init_is_private_and_never_disclosed():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "project_root": str(root),
                    "portable_encryption_key_file": "private/master-key",
                }
            ),
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        status, result = invoke(
            ["--config", str(config_path), "encryption-key-init"]
        )
        key_path = root / "private" / "master-key"
        assert status == 0
        assert result == {
            "created": True,
            "portable_encryption_configured": True,
        }
        key = key_path.read_text(encoding="ascii").strip()
        assert key and key not in json.dumps(result)
        assert str(key_path) not in json.dumps(result)
        assert key_path.stat().st_mode & 0o777 == 0o600

        try:
            invoke(["--config", str(config_path), "encryption-key-init"])
        except SystemExit as exc:
            assert "refusing to overwrite" in str(exc)
        else:
            raise AssertionError("existing portable encryption key was overwritten")


def test_portable_state_export_cli_is_explicit_and_reports_no_plaintext():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.db"
        source_vault = root / "source-autofill.bin"
        destination = root / "export" / "job-search.db"
        destination_vault = root / "export" / "autofill-vault.bin"
        key = root / "export" / "master-key"
        expected = {
            "exported": True,
            "mail_archive_messages": 2,
            "mail_archive_attachments": 1,
            "autofill_vault_exported": False,
            "outlook_token_cache_exported": False,
        }
        with patch(
            "job_search.portable_export.export_portable_state",
            return_value=expected,
        ) as exported:
            status, result = invoke(
                [
                    "--project-root",
                    str(root),
                    "--db",
                    str(source),
                    "--portable-encryption-key-file",
                    str(key),
                    "portable-state-export",
                    "--destination-db",
                    str(destination),
                    "--source-autofill-vault",
                    str(source_vault),
                    "--destination-autofill-vault",
                    str(destination_vault),
                ]
            )
        assert status == 0 and result == expected
        call = exported.call_args.kwargs
        assert call["source_database"] == source.resolve()
        assert call["destination_database"] == destination.resolve()
        assert call["portable_key_file"] == key.resolve()
        assert call["source_autofill_vault"] == source_vault.resolve()
        assert call["destination_autofill_vault"] == destination_vault.resolve()
        assert call["source_archive_key_provider"] is None


def test_resume_cli_reports_setup_and_initializes_only_explicit_private_storage():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        status, report = invoke(["--project-root", str(root), "resume", "doctor"])
        assert status == 2
        assert report["configured"] is False
        assert report["reason"] == "resume_lab_not_configured"
        assert not (root / "resume.db").exists()

        status, listed = invoke(
            [
                "--project-root",
                str(root),
                "--resume-db",
                "private/resume.db",
                "--resume-artifact-root",
                "private/artifacts",
                "resume",
                "list",
            ]
        )
        assert status == 0 and listed["standards"] == []
        assert (root / "private" / "resume.db").stat().st_mode & 0o777 == 0o600
        assert (root / "private" / "artifacts").stat().st_mode & 0o777 == 0o700

        source = root / "resume.tex"
        source.write_text("safe", encoding="utf-8")
        link = root / "resume-link.tex"
        link.symlink_to(source)
        try:
            job_search_app._read_resume_tex(link)
        except ValueError as exc:
            assert "unavailable" in str(exc)
        else:
            raise AssertionError("resume import followed a source symlink")


def test_resume_update_cli_reads_tex_and_preserves_the_standard_identity():
    class FakeGateway:
        def __init__(self) -> None:
            self.calls = []

        def update_standard(self, standard_id, tex_source):
            self.calls.append((standard_id, tex_source))
            return {
                "standard_id": standard_id,
                "standard_version_id": "stdv_new",
                "normalization_status": "ready",
            }

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "updated.tex"
        source.write_text("Updated factual resume", encoding="utf-8")
        gateway = FakeGateway()
        with patch(
            "job_search.resume_lab.gateway.build_resume_lab_gateway",
            return_value=gateway,
        ):
            status, result = invoke(
                [
                    "--project-root",
                    str(root),
                    "--resume-db",
                    "private/resume.db",
                    "--resume-artifact-root",
                    "private/artifacts",
                    "resume",
                    "update",
                    "--standard-id",
                    "std_existing",
                    "--tex",
                    str(source),
                ]
            )
        assert status == 0
        assert result["standard_id"] == "std_existing"
        assert result["standard_version_id"] == "stdv_new"
        assert gateway.calls == [("std_existing", "Updated factual resume")]


def test_status_reports_configured_dependency_failures_without_secret_paths():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "job-search.db"
        invoke(["--project-root", str(root), "--db", str(db_path), "init"])
        missing = root / "private" / "missing-inference.json"
        status, report = invoke(
            [
                "--project-root",
                str(root),
                "--db",
                str(db_path),
                "--inference-config",
                str(missing),
                "status",
            ]
        )
        assert status == 2 and report["status"] == "attention"
        assert report["dependencies"]["issues"] == ["inference_configuration"]
        assert (
            report["dependencies"]["inference"]["status"] == "blocked_setup"
        )
        assert (
            report["dependencies"]["inference"]["remote_mail"]["status"]
            == "disabled"
        )
        assert str(missing) not in json.dumps(report)


def test_status_honors_environment_selected_inference_profile():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "job-search.db"
        credential = root / "remote-key"
        credential.write_text("private-remote-token\n", encoding="ascii")
        credential.chmod(0o600)
        profile = root / "inference.json"
        profile.write_text(
            json.dumps(
                {
                    "version": 1,
                    "profile_id": "environment-only",
                    "structured_generation": {
                        "kind": "openai_compatible",
                        "provider_id": "test-remote",
                        "base_url": "https://inference.example.test/v1",
                        "model": "example/model",
                        "model_revision": "example/model@immutable-commit",
                        "deployment_revision": "worker@sha256-immutable",
                        "credential_file": str(credential),
                        "timeout_seconds": 30,
                        "max_response_bytes": 65536,
                        "max_input_tokens": 32768,
                        "default_max_output_tokens": 4096,
                        "json_schema_mode": True,
                    },
                    "embeddings": None,
                }
            ),
            encoding="utf-8",
        )
        profile.chmod(0o600)
        environment = {"JOB_SEARCH_INFERENCE_CONFIG": str(profile)}
        invoke(
            ["--project-root", str(root), "--db", str(db_path), "init"],
            environment=environment,
        )
        status, report = invoke(
            ["--project-root", str(root), "--db", str(db_path), "status"],
            environment=environment,
        )
        inference = report["dependencies"]["inference"]
        assert status == 0
        assert inference["configured"] is True
        assert inference["configuration_ready"] is True
        assert inference["structured_generation"] is True
        assert report["status"] == "healthy_unprobed"
        assert report["dependencies"]["status"] == "configuration_ready"
        assert inference["status"] == "configuration_ready"
        assert inference["external_endpoint_probed"] is False
        serialized = json.dumps(report)
        assert str(profile) not in serialized
        assert str(credential) not in serialized
        assert "private-remote-token" not in serialized


def test_status_redacts_invalid_environment_inference_path():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "job-search.db"
        environment = {"JOB_SEARCH_INFERENCE_CONFIG": "/" + "sensitive" * 600}
        invoke(
            ["--project-root", str(root), "--db", str(db_path), "init"],
            environment=environment,
        )
        status, report = invoke(
            ["--project-root", str(root), "--db", str(db_path), "status"],
            environment=environment,
        )
        assert status == 2
        assert report["dependencies"]["issues"] == ["inference_configuration"]
        inference = report["dependencies"]["inference"]
        assert inference["configured"] is True
        assert inference["status"] == "blocked_setup"
        assert "sensitive" not in json.dumps(report)


def test_configured_but_unprobed_outlook_is_not_reported_as_fully_healthy():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "job-search.db"
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "project_root": str(root),
                    "application_db": str(db_path),
                    "outlook_client_id": "12345678-1234-1234-1234-123456789abc",
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        invoke(["--config", str(config_path), "init"])
        status, report = invoke(["--config", str(config_path), "status"])
        assert status == 0
        assert report["status"] == "healthy_unprobed"
        assert report["dependencies"]["status"] == "configuration_ready"
        assert report["dependencies"]["outlook"] == {
            "configured": True,
            "status": "configured",
            "authentication_probed": False,
        }


def test_status_reports_remote_mail_egress_opt_in_without_probing_endpoint():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "job-search.db"
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "project_root": str(root),
                    "application_db": str(db_path),
                    "remote_mail_inference_enabled": True,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        invoke(["--config", str(config_path), "init"])

        status, report = invoke(["--config", str(config_path), "status"])
        remote_mail = report["dependencies"]["inference"]["remote_mail"]
        assert status == 2 and report["status"] == "attention"
        assert report["dependencies"]["issues"] == ["remote_mail_inference"]
        assert remote_mail == {
            "egress_enabled": True,
            "active": False,
            "status": "blocked_setup",
            "external_endpoint_probed": False,
        }

        credential = root / "inference-key"
        credential.write_text("private-token\n", encoding="ascii")
        os.chmod(credential, 0o600)
        inference_path = root / "inference.json"
        inference_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "profile_id": "mail-status",
                    "structured_generation": {
                        "kind": "openai_compatible",
                        "provider_id": "test-remote",
                        "base_url": "https://inference.example.test/v1",
                        "model": "example/model",
                        "model_revision": "example/model@immutable-commit",
                        "deployment_revision": "worker@sha256-immutable",
                        "credential_file": str(credential),
                        "timeout_seconds": 30,
                        "max_response_bytes": 65536,
                        "max_input_tokens": 32768,
                        "default_max_output_tokens": 4096,
                        "json_schema_mode": True,
                    },
                    "embeddings": None,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(inference_path, 0o600)
        configured = json.loads(config_path.read_text(encoding="utf-8"))
        configured["inference_config"] = str(inference_path)
        config_path.write_text(json.dumps(configured), encoding="utf-8")
        os.chmod(config_path, 0o600)

        status, report = invoke(["--config", str(config_path), "status"])
        remote_mail = report["dependencies"]["inference"]["remote_mail"]
        assert status == 0 and report["status"] == "healthy_unprobed"
        assert report["dependencies"]["status"] == "configuration_ready"
        assert report["dependencies"]["issues"] == []
        assert remote_mail == {
            "egress_enabled": True,
            "active": True,
            "status": "configuration_ready",
            "external_endpoint_probed": False,
        }


def test_status_redacts_remote_preference_identity_migration_preflight():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "job-search.db"
        preference_db = root / "preference.db"
        credential = root / "remote-key"
        credential.write_text("private-remote-token\n", encoding="ascii")
        credential.chmod(0o600)
        inference_path = root / "inference.json"
        inference_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "profile_id": "travel-test",
                    "structured_generation": None,
                    "embeddings": {
                        "kind": "openai_compatible",
                        "provider_id": "test-remote",
                        "base_url": "https://secret-endpoint.example/v1",
                        "model": "BAAI/bge-base-en-v1.5",
                        "model_revision": "BAAI/bge-base-en-v1.5@immutable-commit",
                        "deployment_revision": "worker@sha256-immutable",
                        "credential_file": str(credential),
                        "timeout_seconds": 30,
                        "max_response_bytes": 65536,
                        "max_input_tokens": 8192,
                        "max_batch_size": 32,
                        "dimensions": 768,
                    },
                }
            ),
            encoding="utf-8",
        )
        inference_path.chmod(0o600)
        from job_search.inference import load_inference_config

        configured_identity = load_inference_config(
            inference_path
        ).embeddings.embedding_identity
        local_identity = "BAAI/bge-base-en-v1.5@local-immutable-revision"
        with sqlite3.connect(preference_db) as connection:
            connection.executescript(
                "CREATE TABLE preference_state (key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE preference_model_runs ("
                "run_id TEXT PRIMARY KEY, model_revision TEXT);"
            )
            connection.execute(
                "INSERT INTO preference_model_runs VALUES (?,?)",
                ("local_champion", local_identity),
            )
            connection.execute(
                "INSERT INTO preference_state VALUES ('champion_run_id',?)",
                ("local_champion",),
            )

        invoke(["--project-root", str(root), "--db", str(db_path), "init"])
        status, report = invoke(
            [
                "--project-root",
                str(root),
                "--db",
                str(db_path),
                "--preference-db",
                str(preference_db),
                "--inference-config",
                str(inference_path),
                "status",
            ]
        )
        preflight = report["dependencies"]["inference"]["preference_embeddings"]
        assert status == 2
        assert report["dependencies"]["inference"]["status"] == "attention"
        assert preflight["status"] == "migration_required"
        assert preflight["champion_present"] is True
        assert preflight["identity_match"] is False
        assert preflight["automatic_migration"] is False
        serialized = json.dumps(report)
        assert local_identity not in serialized
        assert configured_identity not in serialized
        assert "secret-endpoint.example" not in serialized
        assert "private-remote-token" not in serialized
        assert str(credential) not in serialized

        with sqlite3.connect(preference_db) as connection:
            connection.execute(
                "UPDATE preference_model_runs SET model_revision=? WHERE run_id=?",
                (configured_identity, "local_champion"),
            )
        status, report = invoke(
            [
                "--project-root",
                str(root),
                "--db",
                str(db_path),
                "--preference-db",
                str(preference_db),
                "--inference-config",
                str(inference_path),
                "status",
            ]
        )
        preflight = report["dependencies"]["inference"]["preference_embeddings"]
        assert status == 0
        assert preflight["status"] == "ready"
        assert preflight["identity_match"] is True


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ok ({len(tests)} job-search app tests)")


if __name__ == "__main__":
    main()
