#!/usr/bin/env python3
"""Offline contract tests for the Runpod worker-vLLM resume provider."""

from __future__ import annotations

import http.client
import json
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest import mock

from job_search.contracts import ContractError
from job_search.resume_lab.gateway import ResumeLabProductionGateway
from job_search.resume_lab.local_model_driver import build_prompt
from job_search.resume_lab.model import (
    ResumeModelError,
    build_resume_model_request,
    load_resume_model_config,
    resume_model_status,
)
from job_search.resume_lab.runpod_model import (
    MAX_RUNPOD_RESPONSE_BYTES,
    RUNPOD_API_ORIGIN,
    RunpodJsonResumeModel,
    RunpodReconciliationRequired,
    RunpodResumeModelConfig,
    _prompt_token_upper_bound,
)


def expect(error, operation, message: str = ""):
    try:
        operation()
    except error as exc:
        if message and message not in str(exc):
            raise AssertionError(f"expected {message!r} in {str(exc)!r}") from exc
        return exc
    raise AssertionError(f"expected {error.__name__}")


def _write_key(root: Path, name: str = "runpod.key") -> Path:
    path = root / name
    path.write_text("rpa_" + "x" * 48 + "\n")
    path.chmod(0o600)
    return path


def _config(root: Path, **overrides: Any) -> RunpodResumeModelConfig:
    values = {
        "producer_version": "runpod-resume-qwen-v1",
        "endpoint_id": "abc123endpoint",
        "model_id": "Qwen/Qwen2.5-7B-Instruct",
        "model_revision": "a" * 40,
        "worker_image_digest": "sha256:" + "b" * 64,
        "api_key_file": overrides.get("api_key_file") or _write_key(root),
        "request_timeout_seconds": 5,
        "timeout_seconds": 30,
        "poll_interval_seconds": 0.05,
        "context_size": 32768,
        "max_tokens": 1024,
        "temperature": 0.1,
        "top_p": 0.95,
    }
    values.update(overrides)
    return RunpodResumeModelConfig(**values)


def _response(value: Mapping[str, Any]) -> tuple[int, Mapping[str, str], bytes]:
    return 200, {"content-type": "application/json"}, json.dumps(value).encode()


class FakeTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[Mapping[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> tuple[int, Mapping[str, str], bytes]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _model(
    root: Path, transport: FakeTransport
) -> tuple[RunpodJsonResumeModel, list[float]]:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return (
        RunpodJsonResumeModel(
            _config(root),
            transport=transport,
            clock=lambda: now[0],
            sleeper=sleep,
        ),
        now,
    )


def _completion(content: str, *, wrapped: bool = False) -> Mapping[str, Any]:
    value: Mapping[str, Any] = {
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if wrapped:
        return {"status_code": 200, "body": json.dumps(value)}
    return value


def test_remote_config_dispatch_is_owner_only_and_status_is_redacted() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = _write_key(root)
        endpoint = "abc123endpoint"
        config_path = root / "resume-model.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "provider": "runpod_serverless_vllm",
                    "producer_version": "runpod-resume-qwen-v1",
                    "endpoint_id": endpoint,
                    "model_id": "Qwen/Qwen2.5-7B-Instruct",
                    "model_revision": "a" * 40,
                    "worker_image_digest": "sha256:" + "b" * 64,
                    "api_key_file": str(key),
                    "request_timeout_seconds": 5,
                    "timeout_seconds": 30,
                    "poll_interval_seconds": 0.05,
                    "context_size": 32768,
                    "max_tokens": 1024,
                    "temperature": 0.1,
                    "top_p": 0.95,
                }
            )
        )
        config_path.chmod(0o600)
        config = load_resume_model_config(config_path)
        assert isinstance(config, RunpodResumeModelConfig)
        assert isinstance(config.build(), RunpodJsonResumeModel)
        report = resume_model_status(config)
        assert report["status"] == "configuration_ready"
        assert report["external_endpoint_probed"] is False
        assert report["provider"] == "runpod_serverless_vllm"
        serialized = json.dumps(report)
        assert endpoint not in serialized
        assert str(key) not in serialized
        assert "rpa_" not in serialized

        config_path.chmod(0o644)
        expect(
            ContractError,
            lambda: load_resume_model_config(config_path),
            "owner-only",
        )


def test_api_key_must_be_owner_only_regular_and_not_a_symlink() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = _write_key(root)
        key.chmod(0o644)
        config = _config(root, api_key_file=key)
        assert resume_model_status(config)["status"] == "blocked_setup"
        expect(ResumeModelError, lambda: RunpodJsonResumeModel(config), "owner-only")

        key.chmod(0o600)
        link = root / "linked.key"
        link.symlink_to(key)
        linked = _config(root, api_key_file=link)
        assert resume_model_status(linked)["status"] == "blocked_setup"
        expect(ResumeModelError, lambda: RunpodJsonResumeModel(linked), "unsafe")


def test_async_runpod_queue_uses_vllm_proxy_and_existing_validators() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        transport = FakeTransport(
            [
                _response({"id": "job-1", "status": "IN_QUEUE"}),
                _response({"id": "job-1", "status": "IN_PROGRESS"}),
                _response(
                    {
                        "id": "job-1",
                        "status": "COMPLETED",
                        "output": [_completion('{"requirements":[]}')],
                    }
                ),
            ]
        )
        model, _now = _model(root, transport)
        assert model.extract_requirements("Python is required.") == {"requirements": []}
        assert [call["method"] for call in transport.calls] == [
            "POST",
            "GET",
            "GET",
        ]
        submit = transport.calls[0]
        assert submit["url"] == f"{RUNPOD_API_ORIGIN}/v2/abc123endpoint/run"
        assert submit["headers"]["Authorization"].startswith("Bearer rpa_")
        request = json.loads(submit["body"])
        assert request["input"]["route"] == "/v1/chat/completions"
        assert request["input"]["method"] == "POST"
        chat = request["input"]["body"]
        assert chat["model"] == "Qwen/Qwen2.5-7B-Instruct"
        assert chat["stream"] is False and chat["seed"] == 0
        assert [message["role"] for message in chat["messages"]] == [
            "system",
            "user",
        ]
        assert request["policy"]["executionTimeout"] == 30_000


def test_prompt_context_budget_accepts_boundary_and_rejects_before_submit() -> None:
    task = "extract_job_requirements"
    payload = {"job_description": "x" * 1024}
    output_schema = {"requirements": []}
    prompt = build_prompt(
        build_resume_model_request(task, payload, output_schema)
    )
    max_tokens = 64
    exact_context_size = _prompt_token_upper_bound(prompt) + max_tokens
    assert 2_048 <= exact_context_size <= 262_144

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        accepted_transport = FakeTransport(
            [
                _response(
                    {
                        "id": "job-budget",
                        "status": "COMPLETED",
                        "output": [_completion('{"requirements":[]}')],
                    }
                )
            ]
        )
        accepted = RunpodJsonResumeModel(
            _config(
                root,
                context_size=exact_context_size,
                max_tokens=max_tokens,
            ),
            transport=accepted_transport,
        )
        assert accepted._invoke(task, payload, output_schema) == {"requirements": []}
        assert len(accepted_transport.calls) == 1

        rejected_transport = FakeTransport([])
        rejected = RunpodJsonResumeModel(
            _config(
                root,
                context_size=exact_context_size - 1,
                max_tokens=max_tokens,
            ),
            transport=rejected_transport,
        )
        expect(
            ResumeModelError,
            lambda: rejected._invoke(task, payload, output_schema),
            "exceeds the configured context size",
        )
        assert rejected_transport.calls == []


def test_proxy_body_wrapper_is_supported_but_bad_task_output_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        description = "Python is required."
        invalid = {
            "requirements": [
                {
                    "requirement_id": "python",
                    "text": "invented",
                    "kind": "required",
                    "logic": "atomic",
                    "priority": 2,
                    "source_start": 0,
                    "source_end": 8,
                }
            ]
        }
        transport = FakeTransport(
            [
                _response({"id": "job-2", "status": "IN_QUEUE"}),
                _response(
                    {
                        "id": "job-2",
                        "status": "COMPLETED",
                        "output": [_completion(json.dumps(invalid), wrapped=True)],
                    }
                ),
            ]
        )
        model, _now = _model(root, transport)
        expect(
            ResumeModelError,
            lambda: model.extract_requirements(description),
            "exact job-description span",
        )


def test_submit_is_never_retried_but_safe_status_get_is() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        failed_submit = FakeTransport([OSError("private transport detail")])
        model, _now = _model(root, failed_submit)
        error = expect(
            RunpodReconciliationRequired,
            lambda: model.extract_requirements("Python is required."),
            "manual reconciliation",
        )
        assert "private transport detail" not in str(error)
        assert len(failed_submit.calls) == 1

        retried_get = FakeTransport(
            [
                _response({"id": "job-3", "status": "IN_QUEUE"}),
                OSError("temporary"),
                _response(
                    {
                        "id": "job-3",
                        "status": "COMPLETED",
                        "output": [_completion('{"requirements":[]}')],
                    }
                ),
            ]
        )
        model, _now = _model(root, retried_get)
        assert model.extract_requirements("Python is required.") == {"requirements": []}
        assert [call["method"] for call in retried_get.calls] == [
            "POST",
            "GET",
            "GET",
        ]


def test_partial_submit_response_requires_reconciliation() -> None:
    class PartialResponse:
        headers: Mapping[str, str] = {"content-type": "application/json"}

        def __init__(self, url: str) -> None:
            self.url = url

        def geturl(self):
            return self.url

        def getcode(self):
            return 200

        def read(self, _maximum):
            raise http.client.IncompleteRead(b'{"id":', 20)

        def close(self):
            return None

    class PartialOpener:
        calls = 0

        def open(self, request, timeout):
            del timeout
            self.calls += 1
            return PartialResponse(request.full_url)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        opener = PartialOpener()
        with mock.patch(
            "job_search.resume_lab.runpod_model.urllib.request.build_opener",
            return_value=opener,
        ):
            model = RunpodJsonResumeModel(_config(root))
            error = expect(
                RunpodReconciliationRequired,
                lambda: model.extract_requirements("Python is required."),
                "manual reconciliation",
            )
        assert error.job_id == ""
        assert opener.calls == 1


def test_accepted_resume_job_requires_reconciliation_on_bad_status_read() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        bad_responses = (
            (401, {"content-type": "application/json"}, b"{}"),
            (404, {"content-type": "application/json"}, b"{}"),
            (200, {"content-type": "text/html"}, b"{}"),
            (200, {"content-type": "application/json"}, b"not-json"),
            _response({"status": "IN_PROGRESS"}),
        )
        for bad_response in bad_responses:
            transport = FakeTransport(
                [
                    _response({"id": "accepted-resume-job", "status": "IN_QUEUE"}),
                    bad_response,
                ]
            )
            model, _now = _model(root, transport)
            error = expect(
                RunpodReconciliationRequired,
                lambda: model.extract_requirements("Python is required."),
                "job accepted-resume-job",
            )
            assert error.job_id == "accepted-resume-job"
            assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_redirects_large_responses_and_job_identity_changes_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        redirected = FakeTransport([(302, {"location": "https://evil.example"}, b"{}")])
        model, _now = _model(root, redirected)
        expect(
            ResumeModelError,
            lambda: model.extract_requirements("Python is required."),
            "HTTP 302",
        )
        assert len(redirected.calls) == 1

        oversized = FakeTransport([(200, {}, b"x" * (MAX_RUNPOD_RESPONSE_BYTES + 1))])
        model, _now = _model(root, oversized)
        expect(
            RunpodReconciliationRequired,
            lambda: model.extract_requirements("Python is required."),
            "manual reconciliation",
        )

        changed = FakeTransport(
            [
                _response({"id": "job-4", "status": "IN_QUEUE"}),
                _response({"id": "different-job", "status": "IN_PROGRESS"}),
            ]
        )
        model, _now = _model(root, changed)
        changed_error = expect(
            RunpodReconciliationRequired,
            lambda: model.extract_requirements("Python is required."),
            "job job-4",
        )
        assert changed_error.job_id == "job-4"


def test_accepted_job_deadline_requires_reconciliation_before_resubmit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        transport = FakeTransport(
            [_response({"id": "job-deadline", "status": "IN_QUEUE"})]
        )
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        model = RunpodJsonResumeModel(
            _config(
                root,
                timeout_seconds=10,
                request_timeout_seconds=5,
                poll_interval_seconds=10,
            ),
            transport=transport,
            clock=lambda: now[0],
            sleeper=sleep,
        )
        error = expect(
            RunpodReconciliationRequired,
            lambda: model.extract_requirements("Python is required."),
            "job job-deadline",
        )
        assert error.job_id == "job-deadline"
        assert [call["method"] for call in transport.calls] == ["POST"]


def test_completion_model_identity_and_output_limit_are_enforced() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wrong_model = dict(_completion('{"requirements":[]}'))
        wrong_model["model"] = "Different/Model"
        transport = FakeTransport(
            [
                _response(
                    {
                        "id": "job-model",
                        "status": "COMPLETED",
                        "output": [wrong_model],
                    }
                )
            ]
        )
        model, _now = _model(root, transport)
        expect(
            ResumeModelError,
            lambda: model.extract_requirements("Python is required."),
            "invalid completion",
        )

        truncated = json.loads(json.dumps(_completion('{"requirements":[]}')))
        truncated["choices"][0]["finish_reason"] = "length"
        transport = FakeTransport(
            [
                _response(
                    {
                        "id": "job-length",
                        "status": "COMPLETED",
                        "output": [truncated],
                    }
                )
            ]
        )
        model, _now = _model(root, transport)
        expect(
            ResumeModelError,
            lambda: model.extract_requirements("Python is required."),
            "output limit",
        )


def test_remote_provenance_is_immutable_and_contains_no_endpoint_or_secret() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        transport = FakeTransport([])
        model, _now = _model(root, transport)
        gateway = object.__new__(ResumeLabProductionGateway)
        gateway.model = model
        provenance = gateway._model_provenance()
        assert provenance["provider"] == "runpod_serverless_vllm"
        identity = provenance["provider_identity"]
        assert identity["model_revision"] == "a" * 40
        assert identity["worker_image_digest"] == "sha256:" + "b" * 64
        serialized = json.dumps(provenance)
        assert "abc123endpoint" not in serialized
        assert "rpa_" not in serialized
        assert str(model.config.api_key_file) not in serialized
        assert len(provenance["config_sha256"]) == 64


def test_remote_config_rejects_mutable_or_ambiguous_identity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expect(
            ContractError,
            lambda: _config(root, endpoint_id="https://evil.example/x"),
            "endpoint_id",
        )
        expect(
            ContractError,
            lambda: _config(root, model_revision="main"),
            "immutable",
        )
        expect(
            ContractError,
            lambda: _config(root, worker_image_digest="latest"),
            "immutable",
        )


def main() -> None:
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"ok ({len(tests)} Runpod resume model tests)")


if __name__ == "__main__":
    main()
