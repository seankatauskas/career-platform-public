#!/usr/bin/env python3
"""Offline contract tests for portable inference configuration and transports."""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest import mock

from job_search.inference import (
    InferenceConfigError,
    InferenceTransportError,
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleStructuredGenerator,
    RunpodQueuedEmbeddingConfig,
    RunpodQueuedEmbeddingProvider,
    RunpodQueuedGenerationConfig,
    RunpodQueuedStructuredGenerator,
    build_embedding_provider,
    build_structured_provider,
    configured_inference_path,
    load_inference_config,
)
from job_search.inference.providers import (
    _NoRedirectHandler,
    _RETRYABLE_HTTP_STATUSES,
    _validate_runpod_url,
    post_json,
    runpod_request_json,
)


def _profile(root: Path) -> Path:
    credential = root / "api-key"
    credential.write_text("test-secret-token\n", encoding="utf-8")
    credential.chmod(0o600)
    common = {
        "kind": "openai_compatible",
        "provider_id": "test-cloud",
        "base_url": "https://inference.example.test/v1",
        "model": "example/model",
        "model_revision": "example/model@0123456789abcdef",
        "deployment_revision": "worker-vllm@sha256-deadbeef",
        "credential_file": "api-key",
        "timeout_seconds": 30,
        "max_response_bytes": 65536,
        "max_input_tokens": 8192,
    }
    value = {
        "version": 1,
        "profile_id": "test-vacation",
        "structured_generation": {
            **common,
            "default_max_output_tokens": 1024,
            "json_schema_mode": True,
        },
        "embeddings": {
            **common,
            "model": "BAAI/bge-base-en-v1.5",
            "model_revision": "BAAI/bge-base-en-v1.5@0123456789abcdef",
            "max_batch_size": 2,
            "dimensions": 3,
        },
    }
    path = root / "inference.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _queued_profile(root: Path) -> Path:
    credential = root / "runpod-key"
    credential.write_text("rpa_" + "x" * 48 + "\n", encoding="utf-8")
    credential.chmod(0o600)
    value = {
        "version": 1,
        "profile_id": "test-runpod-queue",
        "structured_generation": {
            "kind": "runpod_queued",
            "provider_id": "runpod-generation",
            "endpoint_id": "generation123",
            "worker_protocol": "worker_vllm_proxy_v1",
            "model": "example/model",
            "model_revision": "example/model@" + "a" * 40,
            "deployment_revision": "sha256:" + "b" * 64,
            "credential_file": "runpod-key",
            "request_timeout_seconds": 5,
            "job_timeout_seconds": 30,
            "poll_interval_seconds": 0.25,
            "max_response_bytes": 65536,
            "max_input_tokens": 8192,
            "default_max_output_tokens": 1024,
            "json_schema_mode": True,
        },
        "embeddings": {
            "kind": "runpod_queued",
            "provider_id": "runpod-embeddings",
            "endpoint_id": "embedding123",
            "worker_protocol": "job_search_infinity_exact_v1",
            "model": "BAAI/bge-base-en-v1.5",
            "model_revision": "BAAI/bge-base-en-v1.5@" + "c" * 40,
            "deployment_revision": "sha256:" + "d" * 64,
            "credential_file": "runpod-key",
            "request_timeout_seconds": 5,
            "job_timeout_seconds": 30,
            "poll_interval_seconds": 0.25,
            "max_response_bytes": 65536,
            "max_input_tokens": 8192,
            "max_batch_size": 2,
            "dimensions": 3,
        },
    }
    path = root / "runpod-inference.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


class QueueTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
        maximum: int,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
                "maximum": maximum,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected queue request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _queue_clock() -> tuple[list[float], Any, Any]:
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return now, clock, sleep


def _completion(content: str = '{"answer":true}') -> Mapping[str, Any]:
    return {
        "id": "completion-1",
        "model": "example/model",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        },
    }


class InferenceTests(unittest.TestCase):
    def test_owner_only_profile_resolves_relative_secret_without_exposing_it(
        self
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_inference_config(_profile(root))
            self.assertEqual(config.profile_id, "test-vacation")
            self.assertEqual(
                config.structured_generation.credential_file, root / "api-key"
            )
            rendered = json.dumps(
                config.structured_generation.provenance, sort_keys=True
            )
            self.assertNotIn("test-secret-token", rendered)
            self.assertNotIn("credential", rendered)

    def test_profile_and_secret_reject_unsafe_mode_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _profile(root)
            path.chmod(0o644)
            with self.assertRaisesRegex(InferenceConfigError, "owner-only"):
                load_inference_config(path)
            path.chmod(0o600)
            link = root / "profile-link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(InferenceConfigError, "regular file"):
                load_inference_config(link)
            (root / "api-key").chmod(0o644)
            with self.assertRaisesRegex(InferenceConfigError, "credential.*owner-only"):
                load_inference_config(path)

    def test_explicit_config_path_wins_over_environment(self) -> None:
        previous = os.environ.get("JOB_SEARCH_INFERENCE_CONFIG")
        os.environ["JOB_SEARCH_INFERENCE_CONFIG"] = "/environment/profile.json"
        try:
            self.assertEqual(
                configured_inference_path("/explicit/profile.json"),
                Path("/explicit/profile.json"),
            )
            self.assertEqual(
                configured_inference_path(),
                Path("/environment/profile.json"),
            )
        finally:
            if previous is None:
                os.environ.pop("JOB_SEARCH_INFERENCE_CONFIG", None)
            else:
                os.environ["JOB_SEARCH_INFERENCE_CONFIG"] = previous

    def test_structured_generation_has_bounded_explicit_request_and_provenance(
        self
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(
                _profile(Path(directory))
            ).structured_generation
            calls = []

            def transport(url, headers, body, timeout, maximum):
                calls.append((url, headers, json.loads(body), timeout, maximum))
                return {
                    "id": "response-1",
                    "model": "example/model",
                    "choices": [
                        {
                            "message": {"content": '{"answer":true}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 4,
                        "total_tokens": 16,
                    },
                }

            provider = OpenAICompatibleStructuredGenerator(config, transport)
            result = provider.generate(
                [{"role": "user", "content": "Return a boolean."}],
                json_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "boolean"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                schema_name="answer",
                max_output_tokens=20,
            )
            self.assertEqual(result.text, '{"answer":true}')
            self.assertEqual(result.usage["generation_tokens"], 4)
            self.assertEqual(result.provenance["model_revision"], config.model_revision)
            self.assertEqual(
                result.provenance["generation_identity"],
                provider.generation_identity,
            )
            url, headers, body, timeout, maximum = calls[0]
            self.assertEqual(url, "https://inference.example.test/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer test-secret-token")
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertEqual((timeout, maximum), (30.0, 65536))

            with self.assertRaisesRegex(InferenceTransportError, "token limit"):
                provider.generate(
                    [{"role": "user", "content": "x"}],
                    json_schema={"type": "object"},
                    schema_name="x",
                    max_output_tokens=2048,
                )

    def test_generation_identity_includes_provider_deployment_and_decoding(
        self
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(
                _profile(Path(directory))
            ).structured_generation
            first = OpenAICompatibleStructuredGenerator(config)
            changed_endpoint = OpenAICompatibleStructuredGenerator(
                replace(config, base_url="https://second.example.test/v1")
            )
            changed_deployment = OpenAICompatibleStructuredGenerator(
                replace(config, deployment_revision="worker-vllm@second-digest")
            )
            changed_decoding = OpenAICompatibleStructuredGenerator(
                replace(config, json_schema_mode=False)
            )
            self.assertEqual(first.model_revision, changed_endpoint.model_revision)
            self.assertNotEqual(
                first.generation_identity, changed_endpoint.generation_identity
            )
            self.assertNotEqual(
                first.generation_identity, changed_deployment.generation_identity
            )
            self.assertNotEqual(
                first.generation_identity, changed_decoding.generation_identity
            )

    def test_structured_generation_budgets_the_schema_and_complete_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                load_inference_config(_profile(Path(directory))).structured_generation,
                max_input_tokens=700,
            )
            called = []

            def transport(*args):
                called.append(args)
                return _completion()

            provider = OpenAICompatibleStructuredGenerator(config, transport)
            with self.assertRaisesRegex(InferenceTransportError, "context limit"):
                provider.generate(
                    [{"role": "user", "content": "small"}],
                    json_schema={
                        "type": "object",
                        "description": "x" * 1_000,
                        "additionalProperties": False,
                    },
                    schema_name="large_schema",
                    max_output_tokens=20,
                )
            self.assertEqual(called, [])

    def test_embedding_provider_batches_reorders_and_validates_exact_dimensions(
        self
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(_profile(Path(directory))).embeddings
            calls = []

            def transport(url, headers, body, timeout, maximum):
                del headers, timeout, maximum
                request = json.loads(body)
                calls.append((url, request))
                data = [
                    {"index": index, "embedding": [float(index + 1), 0.0, 0.5]}
                    for index in range(len(request["input"]))
                ]
                return {"data": list(reversed(data)), "model": request["model"]}

            provider = OpenAICompatibleEmbeddingProvider(config, transport)
            vectors = provider.encode(["one", "two", "three"])
            self.assertEqual(len(calls), 2)
            self.assertEqual([len(call[1]["input"]) for call in calls], [2, 1])
            self.assertEqual(
                vectors, [[1.0, 0.0, 0.5], [2.0, 0.0, 0.5], [1.0, 0.0, 0.5]]
            )
            self.assertTrue(
                provider.model_revision.startswith(
                    "BAAI/bge-base-en-v1.5@0123456789abcdef#provider-"
                )
            )
            self.assertEqual(
                provider.provenance["model_revision"], provider.model_revision
            )

            def malformed(url, headers, body, timeout, maximum):
                del url, headers, body, timeout, maximum
                return {
                    "model": config.model,
                    "data": [{"index": 0, "embedding": [1.0]}],
                }

            broken = OpenAICompatibleEmbeddingProvider(config, malformed)
            with self.assertRaisesRegex(InferenceTransportError, "dimension"):
                broken.encode(["one"])

    def test_generation_and_embeddings_require_exact_response_model_attestation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(_profile(Path(directory)))
            invalid_values = (None, "", "different/model", 7, "x" * 256)
            for value in invalid_values:
                completion = dict(_completion())
                if value is None:
                    completion.pop("model")
                else:
                    completion["model"] = value
                generation = OpenAICompatibleStructuredGenerator(
                    config.structured_generation,
                    lambda *_args, response=completion: response,
                )
                with self.subTest(capability="generation", model=value):
                    with self.assertRaisesRegex(
                        InferenceTransportError, "attest the exact configured model"
                    ):
                        generation.generate(
                            [{"role": "user", "content": "answer"}],
                            json_schema={"type": "object"},
                            schema_name="answer",
                            max_output_tokens=20,
                        )

                embedding_payload: dict[str, Any] = {
                    "data": [{"index": 0, "embedding": [1.0, 0.0, 0.5]}]
                }
                if value is not None:
                    embedding_payload["model"] = value
                embeddings = OpenAICompatibleEmbeddingProvider(
                    config.embeddings,
                    lambda *_args, response=embedding_payload: response,
                )
                with self.subTest(capability="embeddings", model=value):
                    with self.assertRaisesRegex(
                        InferenceTransportError, "attest the exact configured model"
                    ):
                        embeddings.encode(["one"])

    def test_embedding_cache_identity_includes_provider_and_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(_profile(Path(directory))).embeddings
            first = OpenAICompatibleEmbeddingProvider(config)
            changed = OpenAICompatibleEmbeddingProvider(
                replace(config, deployment_revision="worker-vllm@different-digest")
            )
            changed_provider = OpenAICompatibleEmbeddingProvider(
                replace(config, provider_id="second-cloud")
            )
            self.assertNotEqual(first.model_revision, changed.model_revision)
            self.assertNotEqual(first.model_revision, changed_provider.model_revision)
            self.assertEqual(
                first.provenance["weights_revision"], config.model_revision
            )
            self.assertEqual(first.provenance["model_revision"], first.model_revision)

    def test_synchronous_provider_never_auto_retries_ambiguous_post(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(_profile(Path(directory)))

            def ambiguous(*_args):
                raise InferenceTransportError(
                    "transport response was lost", retryable=True, status_code=503
                )

            generation = OpenAICompatibleStructuredGenerator(
                config.structured_generation, ambiguous
            )
            with self.assertRaises(InferenceTransportError) as caught:
                generation.generate(
                    [{"role": "user", "content": "answer"}],
                    json_schema={"type": "object"},
                    schema_name="answer",
                    max_output_tokens=20,
                )
            self.assertFalse(caught.exception.retryable)
            self.assertIn("manual retry", str(caught.exception))

            embeddings = OpenAICompatibleEmbeddingProvider(config.embeddings, ambiguous)
            with self.assertRaises(InferenceTransportError) as caught:
                embeddings.encode(["one"])
            self.assertFalse(caught.exception.retryable)
            self.assertIn("manual retry", str(caught.exception))

    def test_nuextract_queue_preserves_template_order_and_bounds_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = load_inference_config(_queued_profile(Path(directory))).structured_generation
            config = replace(base, model="numind/NuExtract3", model_revision="numind/NuExtract3@" + "a" * 40)
            completion = _completion()
            completion["model"] = config.model
            transport = QueueTransport([{"id": "nuextract-1", "status": "COMPLETED", "output": completion}])
            provider = RunpodQueuedStructuredGenerator(config, transport=transport)
            options = dict(json_schema={"type": "object"}, schema_name="extraction", max_output_tokens=20,
                           extraction_template={"z": "number", "a": "verbatim-string"}, extraction_instructions="Extract exact values.")
            provider.generate([{"role": "user", "content": "Text"}], **options)
            body = json.loads(transport.calls[0]["body"])["input"]["body"]
            self.assertNotIn("response_format", body)
            self.assertEqual(body["chat_template_kwargs"], {
                "template": '{"z":"number","a":"verbatim-string"}',
                "instructions": "Extract exact values.", "enable_thinking": False,
            })
            with self.assertRaisesRegex(InferenceTransportError, "context limit"):
                provider.generate([{"role": "user", "content": "Text"}],
                                  **{**options, "extraction_instructions": "x" * 8192})
            self.assertEqual(len(transport.calls), 1)
            with self.assertRaisesRegex(InferenceTransportError, "user-only"):
                provider.generate([{"role": "system", "content": "Text"}], **options)
            with self.assertRaisesRegex(InferenceTransportError, "requires queued"):
                provider.generate([{"role": "user", "content": "Text"}],
                                  json_schema={}, schema_name="extraction", max_output_tokens=20)
            generic = RunpodQueuedStructuredGenerator(base, transport=transport)
            with self.assertRaisesRegex(InferenceTransportError, "does not support"):
                generic.generate([{"role": "user", "content": "Text"}], **options)

    def test_salary_chunks_fit_serialized_requests_with_escaped_text(self) -> None:
        from job_search.salary.llm import HostedSalaryExtractor, _infer_job

        with tempfile.TemporaryDirectory() as directory:
            base = load_inference_config(_queued_profile(Path(directory))).structured_generation
            for model in (base.model, "numind/NuExtract3"):
                with self.subTest(model=model):
                    config = replace(base, model=model, model_revision=model + "@" + "a" * 40)
                    calls = []

                    def transport(method, url, headers, body, timeout, maximum):
                        calls.append(json.loads(body)["input"]["body"])
                        completion = _completion()
                        completion["model"] = model
                        completion["choices"][0]["message"]["content"] = '{"ranges":[]}'
                        return {"id": "chunk-" + str(len(calls)), "status": "COMPLETED", "output": completion}

                    provider = RunpodQueuedStructuredGenerator(config, transport=transport)
                    extractor = HostedSalaryExtractor(provider)
                    row = {"company": "Test", "title": "Engineer", "location": "US",
                           "employmentType": "Full-time", "description": ('Quoted "text" \\ path.\n' * 1200)}
                    _infer_job(row, extractor, extractor.count_tokens, extractor.max_input_tokens,
                               extractor.reserved_input_tokens)
                    self.assertGreater(len(calls), 1)
                    for body in calls:
                        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                        self.assertLessEqual(len(encoded) + 512 + 256, config.max_input_tokens)

    def test_runpod_queue_config_is_strict_and_dispatches_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_inference_config(_queued_profile(root))
            self.assertIsInstance(
                config.structured_generation, RunpodQueuedGenerationConfig
            )
            self.assertIsInstance(config.embeddings, RunpodQueuedEmbeddingConfig)
            self.assertIsInstance(
                build_structured_provider(config), RunpodQueuedStructuredGenerator
            )
            self.assertIsInstance(
                build_embedding_provider(config), RunpodQueuedEmbeddingProvider
            )
            provenance = json.dumps(config.structured_generation.provenance)
            self.assertNotIn("generation123", provenance)
            self.assertIn("https://api.runpod.ai", provenance)
            generation = config.structured_generation
            self.assertNotEqual(
                generation.generation_identity,
                replace(generation, endpoint_id="generation456").generation_identity,
            )
            self.assertNotEqual(
                generation.generation_identity,
                replace(
                    generation, deployment_revision="sha256:" + "e" * 64
                ).generation_identity,
            )

            value = json.loads(_queued_profile(root).read_text(encoding="utf-8"))
            for field, invalid, message in (
                ("endpoint_id", "https://evil.example/x", "endpoint_id"),
                (
                    "worker_protocol",
                    "job_search_infinity_exact_v1",
                    "worker_vllm_proxy_v1",
                ),
                ("model_revision", "example/model@main", "HF commit"),
                ("deployment_revision", "worker:latest", "sha256"),
            ):
                broken = dict(value)
                broken["structured_generation"] = dict(
                    value["structured_generation"], **{field: invalid}
                )
                path = root / f"broken-{field}.json"
                path.write_text(json.dumps(broken), encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaisesRegex(InferenceConfigError, message):
                    load_inference_config(path)

    def test_runpod_queue_generation_submits_once_and_safely_retries_status_get(
        self
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(
                _queued_profile(Path(directory))
            ).structured_generation
            transport = QueueTransport(
                [
                    {"id": "job-1", "status": "IN_QUEUE"},
                    InferenceTransportError(
                        "temporary status failure", retryable=True, status_code=520
                    ),
                    {"id": "job-1", "status": "IN_PROGRESS"},
                    {
                        "id": "job-1",
                        "status": "COMPLETED",
                        "output": [_completion()],
                    },
                ]
            )
            _now, clock, sleep = _queue_clock()
            provider = RunpodQueuedStructuredGenerator(
                config, transport=transport, clock=clock, sleeper=sleep
            )
            result = provider.generate(
                [{"role": "user", "content": "Return a boolean."}],
                json_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "boolean"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                schema_name="answer",
                max_output_tokens=20,
            )
            self.assertEqual(result.text, '{"answer":true}')
            self.assertEqual(
                [call["method"] for call in transport.calls],
                ["POST", "GET", "GET", "GET"],
            )
            self.assertEqual(
                transport.calls[0]["url"],
                "https://api.runpod.ai/v2/generation123/run",
            )
            request = json.loads(transport.calls[0]["body"])
            self.assertEqual(set(request), {"input", "policy"})
            self.assertEqual(set(request["input"]), {"route", "method", "body"})
            self.assertEqual(request["input"]["route"], "/v1/chat/completions")
            self.assertEqual(request["input"]["method"], "POST")
            self.assertIs(request["input"]["body"]["stream"], False)
            self.assertEqual(
                request["input"]["body"]["response_format"]["type"],
                "json_schema",
            )
            self.assertEqual(request["policy"]["executionTimeout"], 30_000)
            self.assertTrue(all(call["timeout"] <= 5 for call in transport.calls))

    def test_runpod_queue_embedding_batches_and_keys_deployment_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(_queued_profile(Path(directory))).embeddings
            responses = []
            for job, count in (("embed-1", 2), ("embed-2", 1)):
                data = [
                    {"index": index, "embedding": [float(index + 1), 0.0, 0.5]}
                    for index in range(count)
                ]
                responses.append(
                    {
                        "id": job,
                        "status": "COMPLETED",
                        "output": {
                            "model": "BAAI/bge-base-en-v1.5",
                            "job_search_model_revision": "c" * 40,
                            "job_search_worker_protocol": (
                                "job_search_infinity_exact_v1"
                            ),
                            "data": list(reversed(data)),
                        },
                    }
                )
            transport = QueueTransport(responses)
            _now, clock, sleep = _queue_clock()
            provider = RunpodQueuedEmbeddingProvider(
                config, transport=transport, clock=clock, sleeper=sleep
            )
            vectors = provider.encode(["one", "two", "three"])
            self.assertEqual(
                vectors,
                [[1.0, 0.0, 0.5], [2.0, 0.0, 0.5], [1.0, 0.0, 0.5]],
            )
            self.assertEqual(
                [call["method"] for call in transport.calls], ["POST", "POST"]
            )
            requests = [json.loads(call["body"]) for call in transport.calls]
            self.assertEqual(
                [set(request) for request in requests],
                [{"input", "policy"}, {"input", "policy"}],
            )
            self.assertEqual(
                [set(request["input"]) for request in requests],
                [{"model", "input"}, {"model", "input"}],
            )
            self.assertEqual(
                [request["input"]["model"] for request in requests],
                ["BAAI/bge-base-en-v1.5", "BAAI/bge-base-en-v1.5"],
            )
            self.assertEqual(
                [len(request["input"]["input"]) for request in requests], [2, 1]
            )
            changed = replace(config, deployment_revision="sha256:" + "e" * 64)
            self.assertNotEqual(provider.model_revision, changed.embedding_identity)

            unattested = RunpodQueuedEmbeddingProvider(
                config,
                transport=QueueTransport(
                    [
                        {
                            "id": "embed-unattested",
                            "status": "COMPLETED",
                            "output": {
                                "model": "BAAI/bge-base-en-v1.5",
                                "data": [
                                    {
                                        "index": 0,
                                        "embedding": [1.0, 0.0, 0.5],
                                    }
                                ],
                            },
                        }
                    ]
                ),
                clock=clock,
                sleeper=sleep,
            )
            with self.assertRaisesRegex(InferenceTransportError, "attest"):
                unattested.encode(["one"])

    def test_runpod_queue_never_retries_submit_and_rejects_bad_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(
                _queued_profile(Path(directory))
            ).structured_generation
            _now, clock, sleep = _queue_clock()
            failed = QueueTransport(
                [InferenceTransportError("ambiguous submit", retryable=True)]
            )
            provider = RunpodQueuedStructuredGenerator(
                config, transport=failed, clock=clock, sleeper=sleep
            )
            with self.assertRaisesRegex(
                InferenceTransportError, "reconciliation"
            ) as caught:
                provider.generate(
                    [{"role": "user", "content": "answer"}],
                    json_schema={"type": "object"},
                    schema_name="answer",
                    max_output_tokens=20,
                )
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(len(failed.calls), 1)
            self.assertEqual(failed.calls[0]["method"], "POST")

            invalid_success = QueueTransport(
                [InferenceTransportError("invalid successful JSON", retryable=False)]
            )
            provider = RunpodQueuedStructuredGenerator(
                config, transport=invalid_success, clock=clock, sleeper=sleep
            )
            with self.assertRaisesRegex(
                InferenceTransportError, "reconciliation"
            ) as caught:
                provider.generate(
                    [{"role": "user", "content": "answer"}],
                    json_schema={"type": "object"},
                    schema_name="answer",
                    max_output_tokens=20,
                )
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(len(invalid_success.calls), 1)

            invalid = QueueTransport(
                [
                    {
                        "id": "job-invalid",
                        "status": "COMPLETED",
                        "output": [_completion(), _completion()],
                    }
                ]
            )
            provider = RunpodQueuedStructuredGenerator(
                config, transport=invalid, clock=clock, sleeper=sleep
            )
            with self.assertRaisesRegex(InferenceTransportError, "envelope"):
                provider.generate(
                    [{"role": "user", "content": "answer"}],
                    json_schema={"type": "object"},
                    schema_name="answer",
                    max_output_tokens=20,
                )

    def test_accepted_runpod_job_requires_reconciliation_on_bad_status_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(
                _queued_profile(Path(directory))
            ).structured_generation
            for failure in (
                InferenceTransportError(
                    "status returned HTTP 401", retryable=False, status_code=401
                ),
                InferenceTransportError(
                    "status returned HTTP 404", retryable=False, status_code=404
                ),
                InferenceTransportError("status returned invalid JSON", retryable=False),
                InferenceTransportError(
                    "status returned the wrong content type", retryable=False
                ),
            ):
                _now, clock, sleep = _queue_clock()
                transport = QueueTransport(
                    [{"id": "accepted-job", "status": "IN_QUEUE"}, failure]
                )
                provider = RunpodQueuedStructuredGenerator(
                    config, transport=transport, clock=clock, sleeper=sleep
                )
                with self.assertRaises(InferenceTransportError) as caught:
                    provider.generate(
                        [{"role": "user", "content": "answer"}],
                        json_schema={"type": "object"},
                        schema_name="answer",
                        max_output_tokens=20,
                    )
                self.assertFalse(caught.exception.retryable)
                self.assertIn("reconciliation", str(caught.exception))
                self.assertIn("accepted-job", str(caught.exception))
                self.assertEqual(
                    [call["method"] for call in transport.calls], ["POST", "GET"]
                )

    def test_runpod_terminal_statuses_preserve_safe_retry_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(
                _queued_profile(Path(directory))
            ).structured_generation
            for status, retryable in (
                ("FAILED", True),
                ("TIMED_OUT", True),
                ("CANCELLED", False),
            ):
                _now, clock, sleep = _queue_clock()
                provider = RunpodQueuedStructuredGenerator(
                    config,
                    transport=QueueTransport(
                        [{"id": "terminal-job", "status": status}]
                    ),
                    clock=clock,
                    sleeper=sleep,
                )
                with self.assertRaises(InferenceTransportError) as caught:
                    provider.generate(
                        [{"role": "user", "content": "answer"}],
                        json_schema={"type": "object"},
                        schema_name="answer",
                        max_output_tokens=20,
                    )
                self.assertEqual(caught.exception.retryable, retryable)

    def test_runpod_queue_bounds_envelopes_deadline_origin_and_retry_statuses(
        self
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_inference_config(
                _queued_profile(Path(directory))
            ).structured_generation
            now, clock, sleep = _queue_clock()
            too_large = QueueTransport(
                [{"id": "job-large", "status": "IN_QUEUE", "padding": "x" * 70_000}]
            )
            provider = RunpodQueuedStructuredGenerator(
                config, transport=too_large, clock=clock, sleeper=sleep
            )
            with self.assertRaisesRegex(InferenceTransportError, "reconciliation"):
                provider.generate(
                    [{"role": "user", "content": "answer"}],
                    json_schema={"type": "object"},
                    schema_name="answer",
                    max_output_tokens=20,
                )

            pending = QueueTransport([{"id": "job-time", "status": "IN_QUEUE"}])
            provider = RunpodQueuedStructuredGenerator(
                config, transport=pending, clock=clock, sleeper=sleep
            )
            now[0] = 0

            def expire(_seconds: float) -> None:
                now[0] = 31

            provider._queued_transport._sleep = expire
            with self.assertRaises(InferenceTransportError) as caught:
                provider.generate(
                    [{"role": "user", "content": "answer"}],
                    json_schema={"type": "object"},
                    schema_name="answer",
                    max_output_tokens=20,
                )
            self.assertFalse(caught.exception.retryable)
            self.assertIn("job-time", str(caught.exception))
            self.assertIn("reconciliation", str(caught.exception))
            self.assertEqual([call["method"] for call in pending.calls], ["POST"])

            for invalid_url in (
                "https://evil.example/v2/endpoint/run",
                "http://api.runpod.ai/v2/endpoint/run",
                "https://api.runpod.ai.evil.test/v2/endpoint/run",
                "https://api.runpod.ai/v2/endpoint/run?redirect=1",
            ):
                with self.assertRaisesRegex(InferenceTransportError, "URL"):
                    _validate_runpod_url(invalid_url)
            _validate_runpod_url("https://api.runpod.ai/v2/endpoint/run")
            _validate_runpod_url("https://api.runpod.ai/v2/endpoint/status/job-1")
            self.assertTrue(set(range(520, 525)).issubset(_RETRYABLE_HTTP_STATUSES))

    def test_redirects_are_not_followed(self) -> None:
        handler = _NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(None, None, 307, "redirect", {}, "https://other")
        )

    def test_base_synchronous_transport_never_auto_retries_a_post(self) -> None:
        class FailingOpener:
            def __init__(self, status: int) -> None:
                self.status = status

            def open(self, request, timeout):
                del timeout
                raise urllib.error.HTTPError(
                    request.full_url, self.status, "failure", {}, None
                )

        for status in (*range(520, 525), 400):
            with mock.patch(
                "job_search.inference.providers.urllib.request.build_opener",
                return_value=FailingOpener(status),
            ):
                with self.assertRaises(InferenceTransportError) as caught:
                    post_json(
                        "https://inference.example.test/v1/chat/completions",
                        {},
                        b"{}",
                        1,
                        1024,
                    )
            self.assertFalse(caught.exception.retryable)

    def test_partial_http_reads_and_pathological_json_fail_inside_transport(self) -> None:
        class Response:
            def __init__(self, url: str, body: bytes | None = None) -> None:
                self.url = url
                self.body = body
                self.headers = {"content-type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return self.url

            def read(self, _maximum):
                if self.body is None:
                    raise http.client.IncompleteRead(b"{", 20)
                return self.body

        class Opener:
            def __init__(self, body: bytes | None = None) -> None:
                self.body = body

            def open(self, request, timeout):
                del timeout
                return Response(request.full_url, self.body)

        with mock.patch(
            "job_search.inference.providers.urllib.request.build_opener",
            return_value=Opener(),
        ):
            with self.assertRaises(InferenceTransportError) as caught:
                post_json("https://inference.example.test/v1/chat/completions", {}, b"{}", 1, 1024)
            self.assertFalse(caught.exception.retryable)
            self.assertIn("ambiguous", str(caught.exception))

        with mock.patch(
            "job_search.inference.providers.urllib.request.build_opener",
            return_value=Opener(),
        ):
            with self.assertRaises(InferenceTransportError) as caught:
                runpod_request_json(
                    "POST",
                    "https://api.runpod.ai/v2/endpoint/run",
                    {},
                    b"{}",
                    1,
                    1024,
                )
            self.assertTrue(caught.exception.retryable)

        transports = (
            lambda: post_json("https://inference.example.test/v1/chat/completions", {}, b"{}", 1, 16384),
            lambda: runpod_request_json("POST", "https://api.runpod.ai/v2/endpoint/run", {}, b"{}", 1, 16384),
        )
        for body in (b'{"value":NaN}', b"[" * 1100 + b"0" + b"]" * 1100,
                     b'{"value":' * 129 + b"0" + b"}" * 129):
            for transport in transports:
                with mock.patch(
                    "job_search.inference.providers.urllib.request.build_opener",
                    return_value=Opener(body),
                ):
                    with self.assertRaisesRegex(InferenceTransportError, "invalid JSON") as caught:
                        transport()
                    self.assertFalse(caught.exception.retryable)
        for transport in transports:
            with mock.patch(
                "job_search.inference.providers.urllib.request.build_opener",
                return_value=Opener(b'{"value":' * 128 + b'"brackets [] {} are text"' + b"}" * 128),
            ):
                self.assertIsInstance(transport(), dict)


if __name__ == "__main__":
    unittest.main()
