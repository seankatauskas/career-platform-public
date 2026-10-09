"""Dependency-free OpenAI-compatible inference providers with bounded HTTP."""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional, Union
from urllib.parse import urlsplit

from .config import (
    RUNPOD_API_ORIGIN,
    RUNPOD_INFINITY_EMBEDDING_PROTOCOL,
    RUNPOD_VLLM_PROXY_PROTOCOL,
    OpenAIEmbeddingConfig,
    OpenAIGenerationConfig,
    RunpodQueuedEmbeddingConfig,
    RunpodQueuedGenerationConfig,
    load_credential,
)
from .contracts import GenerationResult, InferenceTransportError, InferenceResponseRejected
from .usage import begin_invocation, current_scope, InvocationPending, UsageDeferred, heartbeat_scope

Transport = Callable[[str, Mapping[str, str], bytes, float, int], Mapping[str, Any]]
RunpodTransport = Callable[
    [str, str, Mapping[str, str], Optional[bytes], float, int], Mapping[str, Any]
]
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_JSON_DEPTH = 128
INFERENCE_REQUEST_TOKEN_RESERVE = 256
_RETRYABLE_HTTP_STATUSES = frozenset(
    {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)
_RUNPOD_ENDPOINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_RUNPOD_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_RUNPOD_PENDING = frozenset({"IN_QUEUE", "IN_PROGRESS"})
_RUNPOD_RETRYABLE_FAILURE = frozenset({"FAILED", "TIMED_OUT"})
_RUNPOD_PERMANENT_FAILURE = frozenset({"CANCELLED"})
MAX_RUNPOD_STATUS_POLLS = 20_000


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


def _bounded_read(response: Any, maximum: int) -> bytes:
    header = response.headers.get("Content-Length")
    if header:
        try:
            size = int(header)
            if size < 0 or size > maximum:
                raise InferenceTransportError(
                    "inference response exceeded the configured byte limit",
                    retryable=False,
                )
        except ValueError as exc:
            raise InferenceTransportError(
                "inference response had an invalid Content-Length",
                retryable=False,
            ) from exc
    body = response.read(maximum + 1)
    if len(body) > maximum:
        raise InferenceTransportError(
            "inference response exceeded the configured byte limit",
            retryable=False,
        )
    return body


def _reject_nonfinite_json(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _load_response_json(raw: bytes) -> Any:
    payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_nonfinite_json)
    # Decoder recursion limits differ across Python versions. Enforce our own
    # bound before downstream validators or encoders traverse provider content.
    # Iterators keep this check's extra memory proportional to nesting depth.
    pending = [(iter((payload,)), 0)]
    while pending:
        values, depth = pending[-1]
        try:
            value = next(values)
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, (dict, list)):
            if depth >= MAX_RESPONSE_JSON_DEPTH:
                raise ValueError("JSON nesting exceeded the response depth limit")
            pending.append((iter(value.values() if isinstance(value, dict) else value), depth + 1))
    return payload


def post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> Mapping[str, Any]:
    """POST one JSON request without redirects, unbounded reads, or hidden retries."""

    if len(body) > MAX_REQUEST_BYTES:
        raise InferenceTransportError(
            "inference request exceeded the hard byte limit",
            retryable=False,
        )
    request = urllib.request.Request(
        url, data=body, method="POST", headers=dict(headers)
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = _bounded_read(response, max_response_bytes)
    except urllib.error.HTTPError as exc:
        raise InferenceTransportError(
            f"inference endpoint returned HTTP {exc.code}",
            # This function only performs POSTs and has no portable provider-side
            # idempotency key. Even apparently transient responses are therefore an
            # operator decision, never an automatic durable-queue retry.
            retryable=False,
            status_code=exc.code,
        ) from exc
    except (http.client.HTTPException, OSError, urllib.error.URLError, TimeoutError) as exc:
        raise InferenceTransportError(
            "synchronous inference submission outcome is ambiguous; inspect the "
            "provider before a manual retry",
            retryable=False,
        ) from exc
    try:
        payload = _load_response_json(raw)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise InferenceTransportError(
            "inference endpoint returned invalid JSON",
            retryable=False,
        ) from exc
    if not isinstance(payload, Mapping):
        raise InferenceTransportError(
            "inference endpoint response must be a JSON object",
            retryable=False,
        )
    return payload


def _validate_runpod_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise InferenceTransportError(
            "Runpod inference URL is invalid", retryable=False
        ) from None
    path = parsed.path
    pieces = path.split("/")
    valid_path = False
    if len(pieces) == 5 and pieces[:2] == ["", "v2"]:
        valid_path = bool(
            _RUNPOD_ENDPOINT_ID.fullmatch(pieces[2])
            and pieces[3] == "status"
            and _RUNPOD_JOB_ID.fullmatch(pieces[4])
        )
    elif len(pieces) == 4:
        valid_path = bool(
            pieces[:2] == ["", "v2"]
            and _RUNPOD_ENDPOINT_ID.fullmatch(pieces[2])
            and pieces[3] == "run"
        )
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.runpod.ai"
        or parsed.hostname != "api.runpod.ai"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not valid_path
    ):
        raise InferenceTransportError(
            "Runpod inference URL is invalid", retryable=False
        )


def runpod_request_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    max_response_bytes: int,
) -> Mapping[str, Any]:
    """Execute one fixed-origin Runpod request with no redirects or retries."""

    _validate_runpod_url(url)
    if method not in {"GET", "POST"} or (method == "GET") != (body is None):
        raise InferenceTransportError(
            "Runpod inference request method is invalid", retryable=False
        )
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise InferenceTransportError(
            "Runpod inference request exceeded the hard byte limit", retryable=False
        )
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=dict(headers),
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.geturl() != url:
                raise InferenceTransportError(
                    "Runpod inference refused a redirected response", retryable=False
                )
            content_type = response.headers.get("Content-Type")
            if content_type is not None and not content_type.lower().startswith(
                "application/json"
            ):
                raise InferenceTransportError(
                    "Runpod inference response is not JSON", retryable=False
                )
            raw = _bounded_read(response, max_response_bytes)
    except InferenceTransportError:
        raise
    except urllib.error.HTTPError as exc:
        raise InferenceTransportError(
            f"Runpod inference endpoint returned HTTP {exc.code}",
            retryable=exc.code in _RETRYABLE_HTTP_STATUSES,
            status_code=exc.code,
        ) from exc
    except (
        http.client.HTTPException,
        OSError,
        urllib.error.URLError,
        TimeoutError,
    ) as exc:
        raise InferenceTransportError(
            "Runpod inference endpoint was unavailable", retryable=True
        ) from exc

    try:
        payload = _load_response_json(raw)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise InferenceTransportError(
            "Runpod inference endpoint returned invalid JSON", retryable=False
        ) from exc
    if not isinstance(payload, Mapping):
        raise InferenceTransportError(
            "Runpod inference response must be a JSON object", retryable=False
        )
    return payload


def _body(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise InferenceTransportError(
            "inference request was not serializable JSON",
            retryable=False,
        ) from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise InferenceTransportError(
            "inference request exceeded the hard byte limit",
            retryable=False,
        )
    return encoded


def _usage(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return {}
    output: dict[str, Any] = {}
    for source, target in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "generation_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = raw.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            output[target] = value
    return output


RunpodQueueConfig = Union[RunpodQueuedGenerationConfig, RunpodQueuedEmbeddingConfig]


class _RunpodQueuedOpenAITransport:
    """Adapt a validated OpenAI request to one pinned Runpod worker protocol."""

    def __init__(
        self,
        config: RunpodQueueConfig,
        route: str,
        *,
        transport: RunpodTransport = runpod_request_json,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(
            config, (RunpodQueuedGenerationConfig, RunpodQueuedEmbeddingConfig)
        ):
            raise TypeError("Runpod queue configuration is invalid")
        if isinstance(config, RunpodQueuedGenerationConfig):
            expected_route = "/chat/completions"
            expected_protocol = RUNPOD_VLLM_PROXY_PROTOCOL
        else:
            expected_route = "/embeddings"
            expected_protocol = RUNPOD_INFINITY_EMBEDDING_PROTOCOL
        if route != expected_route or config.worker_protocol != expected_protocol:
            raise ValueError("Runpod worker protocol does not match its capability")
        _validate_runpod_url(f"{RUNPOD_API_ORIGIN}/v2/{config.endpoint_id}/run")
        self.config = config
        self.route = route
        self._transport = transport
        self._clock = clock
        self._sleep = sleeper

    @staticmethod
    def _job_id(value: Any) -> str:
        if not isinstance(value, str) or not _RUNPOD_JOB_ID.fullmatch(value):
            raise InferenceTransportError(
                "Runpod inference returned an invalid job id", retryable=False
            )
        return value

    @staticmethod
    def _reconciliation_required(job_id: str = "") -> InferenceTransportError:
        if job_id and current_scope() is not None:
            return InvocationPending()
        detail = f" for job {job_id}" if job_id else ""
        return InferenceTransportError(
            "Runpod inference submission outcome requires manual reconciliation"
            f"{detail} before retry",
            retryable=False,
        )

    def _bounded_envelope(self, value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise InferenceTransportError(
                "Runpod inference response must be a JSON object", retryable=False
            )
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
            raise InferenceTransportError(
                "Runpod inference response was not finite JSON", retryable=False
            ) from exc
        if len(encoded) > self.config.max_response_bytes:
            raise InferenceTransportError(
                "Runpod inference response exceeded the configured byte limit",
                retryable=False,
            )
        return value

    def _request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        deadline: float,
    ) -> Mapping[str, Any]:
        heartbeat_scope()
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise InferenceTransportError(
                "Runpod inference job exceeded its deadline", retryable=True
            )
        url = RUNPOD_API_ORIGIN + path
        encoded = _body(body) if body is not None else None
        request_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": str(headers.get("Authorization") or ""),
            "Content-Type": "application/json",
            "User-Agent": "job-search-inference-runpod/1.0",
        }
        if not request_headers["Authorization"].startswith("Bearer "):
            raise InferenceTransportError(
                "Runpod inference credential is unavailable", retryable=False
            )
        try:
            result = self._transport(
                method,
                url,
                request_headers,
                encoded,
                min(self.config.request_timeout_seconds, remaining),
                self.config.max_response_bytes,
            )
        except InferenceTransportError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise InferenceTransportError(
                "Runpod inference endpoint was unavailable", retryable=True
            ) from exc
        return self._bounded_envelope(result)

    def _wait(self, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise InferenceTransportError(
                "Runpod inference job exceeded its deadline", retryable=True
            )
        self._sleep(min(self.config.poll_interval_seconds, remaining))

    @staticmethod
    def _output(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        if "output" not in envelope:
            raise InferenceTransportError(
                "Runpod inference completed without output", retryable=False
            )
        output = envelope["output"]
        if isinstance(output, list):
            if len(output) != 1:
                raise InferenceTransportError(
                    "Runpod inference output envelope is invalid", retryable=False
                )
            output = output[0]
        if not isinstance(output, Mapping) or "error" in output:
            raise InferenceTransportError(
                "Runpod inference worker returned invalid output", retryable=False
            )
        return output

    def _poll(
        self,
        first: Mapping[str, Any],
        job_id: str,
        headers: Mapping[str, str],
        deadline: float,
        invocation: Any = None,
    ) -> Mapping[str, Any]:
        envelope = first
        max_polls = min(
            MAX_RUNPOD_STATUS_POLLS,
            math.ceil(
                self.config.job_timeout_seconds / self.config.poll_interval_seconds
            )
            + 1,
        )
        polls = 0
        while True:
            try:
                reported_job_id = self._job_id(envelope.get("id"))
            except InferenceTransportError:
                if invocation is not None:
                    invocation.unavailable()
                raise self._reconciliation_required(job_id) from None
            if reported_job_id != job_id:
                if invocation is not None:
                    invocation.unavailable()
                raise self._reconciliation_required(job_id)
            status = envelope.get("status")
            if not isinstance(status, str):
                if invocation is not None:
                    invocation.unavailable()
                raise self._reconciliation_required(job_id)
            if status == "COMPLETED":
                if invocation is not None:
                    invocation.terminal("completed")
                return self._output(envelope)
            if status in _RUNPOD_RETRYABLE_FAILURE:
                if invocation is not None:
                    invocation.terminal("failed")
                raise InferenceTransportError(
                    f"Runpod inference job ended with status {status}",
                    retryable=True,
                )
            if status in _RUNPOD_PERMANENT_FAILURE:
                if invocation is not None:
                    invocation.terminal("cancelled")
                raise InferenceTransportError(
                    f"Runpod inference job ended with status {status}",
                    retryable=False,
                )
            if status not in _RUNPOD_PENDING:
                if invocation is not None:
                    invocation.unavailable()
                raise self._reconciliation_required(job_id)
            if polls >= max_polls:
                raise self._reconciliation_required(job_id)
            try:
                self._wait(deadline)
            except InferenceTransportError:
                raise self._reconciliation_required(job_id) from None
            polls += 1
            try:
                envelope = self._request(
                    "GET",
                    f"/v2/{self.config.endpoint_id}/status/{job_id}",
                    headers,
                    None,
                    deadline,
                )
            except InferenceTransportError as exc:
                # Status reads are idempotent. Retry only failures explicitly marked
                # retryable, always under the same wall-clock and poll-count bounds.
                if exc.retryable:
                    continue
                # Once Runpod has accepted a job ID, even a permanent-looking GET
                # failure leaves the remote job's state unknown.  Requiring explicit
                # reconciliation prevents a later queue retry from issuing a second
                # billable submission.
                if invocation is not None:
                    invocation.unavailable()
                raise self._reconciliation_required(job_id) from None

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]:
        del timeout_seconds
        if (
            url != self.config.base_url + self.route
            or max_response_bytes != self.config.max_response_bytes
        ):
            raise InferenceTransportError(
                "Runpod inference route contract is invalid", retryable=False
            )
        try:
            openai_body = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise InferenceTransportError(
                "Runpod OpenAI request body is invalid", retryable=False
            ) from exc
        if not isinstance(openai_body, Mapping):
            raise InferenceTransportError(
                "Runpod OpenAI request body must be an object", retryable=False
            )
        openai_body = dict(openai_body)
        if self.route == "/chat/completions":
            openai_body["stream"] = False
            worker_input: Mapping[str, Any] = {
                "route": "/v1/chat/completions",
                "method": "POST",
                "body": openai_body,
            }
        else:
            if (
                set(openai_body) != {"model", "input", "encoding_format"}
                or openai_body.get("model") != self.config.model
                or openai_body.get("encoding_format") != "float"
                or not isinstance(openai_body.get("input"), list)
            ):
                raise InferenceTransportError(
                    "Runpod Infinity embedding request contract is invalid",
                    retryable=False,
                )
            # The exact-revision Infinity derivative retains the native queue API,
            # rather than worker-vLLM's proxy: input is exactly model+input.
            worker_input = {
                "model": openai_body["model"],
                "input": openai_body["input"],
            }
        run_request = {
            "input": worker_input,
            "policy": {
                "executionTimeout": int(self.config.job_timeout_seconds * 1000),
                "ttl": int(min(604_800, self.config.job_timeout_seconds + 300) * 1000),
            },
        }
        deadline = self._clock() + self.config.job_timeout_seconds
        identity = self.config.generation_identity if isinstance(self.config, RunpodQueuedGenerationConfig) else self.config.embedding_identity
        invocation = begin_invocation(identity, "generation" if self.route == "/chat/completions" else "embeddings",
                                      body, reserved_tokens=len(body) + int(openai_body.get("max_tokens", 0)) + INFERENCE_REQUEST_TOKEN_RESERVE,
                                      retrieval_kind="runpod_job")
        if invocation is not None and invocation.job_id:
            try:
                first = self._request("GET", f"/v2/{self.config.endpoint_id}/status/{invocation.job_id}", headers, None, deadline)
            except InferenceTransportError as exc:
                if not exc.retryable:
                    invocation.unavailable()
                raise InvocationPending() from None
            return self._poll(first, invocation.job_id, headers, deadline, invocation)
        if invocation is not None:
            invocation.submitting()
        # Deliberately submit exactly once: an I/O failure after acceptance is
        # ambiguous and an automatic second POST could duplicate work and spend.
        try:
            first = self._request(
                "POST",
                f"/v2/{self.config.endpoint_id}/run",
                headers,
                run_request,
                deadline,
            )
        except InferenceTransportError as exc:
            # A clear 4xx validation/authentication rejection cannot have created a
            # job. Everything else—including an invalid 2xx body—may represent
            # accepted work whose identifier was lost.
            definitive_rejection = (
                exc.status_code is not None
                and 400 <= exc.status_code < 500
                and exc.status_code not in {408, 409, 425, 429}
            )
            if definitive_rejection:
                if invocation is not None:
                    invocation.terminal("failed")
                raise
            if invocation is not None:
                invocation.unknown()
            raise self._reconciliation_required() from None
        try:
            job_id = self._job_id(first.get("id"))
        except InferenceTransportError:
            # A successful POST response without a trustworthy id can still represent
            # accepted work.  It must never turn into an automatic second submission.
            if invocation is not None:
                invocation.unknown()
            raise self._reconciliation_required() from None
        if invocation is not None:
            invocation.accepted(job_id)
        return self._poll(first, job_id, headers, deadline, invocation)


def _managed_sync_transport(transport: Transport, identity: str, capability: str) -> Transport:
    def request(url: str, headers: Mapping[str, str], body: bytes, timeout: float, maximum: int) -> Mapping[str, Any]:
        raw = json.loads(body.decode("utf-8"))
        invocation = begin_invocation(identity, capability, body,
                                      reserved_tokens=len(body) + int(raw.get("max_tokens", 0)) + INFERENCE_REQUEST_TOKEN_RESERVE)
        if invocation is not None:
            invocation.submitting()
        try:
            heartbeat_scope()
            result = transport(url, headers, body, timeout, maximum)
        except Exception as exc:
            if invocation is not None:
                status = getattr(exc, "status_code", None)
                definitive = status is not None and 400 <= status < 500 and status not in {408, 409, 425, 429}
                invocation.terminal("failed") if definitive else invocation.unknown()
            raise
        if invocation is not None:
            invocation.terminal("completed", observed_tokens=_usage(result).get("total_tokens"))
        return result
    return request


class OpenAICompatibleStructuredGenerator:
    def __init__(
        self,
        config: OpenAIGenerationConfig,
        transport: Transport = post_json,
    ) -> None:
        self.config = config
        self._transport = _managed_sync_transport(transport, config.generation_identity, "generation") if isinstance(config, OpenAIGenerationConfig) else transport
        self.model_revision = config.model_revision
        self.generation_identity = config.generation_identity
        self.max_input_tokens = config.max_input_tokens

    @property
    def provenance(self) -> Mapping[str, Any]:
        return self.config.provenance

    @staticmethod
    def count_tokens_upper_bound(text: str) -> int:
        # Every tokenizer token consumes at least one input byte.  UTF-8 bytes are a
        # deliberately conservative, tokenizer-independent upper bound.
        return len(str(text).encode("utf-8"))

    def generate(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
        temperature: float = 0.0,
        extraction_template: Mapping[str, Any] | None = None,
        extraction_instructions: str | None = None,
    ) -> GenerationResult:
        if not 1 <= max_output_tokens <= self.config.default_max_output_tokens:
            raise InferenceTransportError(
                "requested output exceeds the configured token limit",
                retryable=False,
            )
        if not 0.0 <= float(temperature) <= 2.0 or not math.isfinite(
            float(temperature)
        ):
            raise InferenceTransportError(
                "temperature is out of range", retryable=False
            )
        normalized_messages: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(
                content, str
            ):
                raise InferenceTransportError(
                    "inference messages violate the structured-generation contract",
                    retryable=False,
                )
            normalized_messages.append({"role": str(role), "content": content})
        if not normalized_messages or len(normalized_messages) > 32:
            raise InferenceTransportError(
                "inference messages must be a bounded non-empty sequence",
                retryable=False,
            )
        if not isinstance(schema_name, str) or not schema_name or len(schema_name) > 64:
            raise InferenceTransportError("schema name is invalid", retryable=False)
        if not isinstance(json_schema, Mapping):
            raise InferenceTransportError("JSON schema is invalid", retryable=False)
        response_format: dict[str, Any]
        if self.config.json_schema_mode:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": dict(json_schema),
                },
            }
        else:
            response_format = {"type": "json_object"}
        request_body = {
            "model": self.config.model,
            "messages": normalized_messages,
            "temperature": float(temperature),
            "max_tokens": max_output_tokens,
            "response_format": response_format,
        }
        if self.config.model == "numind/NuExtract3":
            if (
                not isinstance(self.config, RunpodQueuedGenerationConfig)
                or not isinstance(extraction_template, Mapping)
                or not extraction_template
                or not isinstance(extraction_instructions, str)
                or not extraction_instructions.strip()
                or any(message["role"] != "user" for message in normalized_messages)
            ):
                raise InferenceTransportError(
                    "NuExtract3 requires queued inference, an extraction template, "
                    "instructions, and user-only input", retryable=False,
                )
            # Match the local frozen extraction prompt. NuExtract's template is
            # distinct from JSON Schema; grammar forcing changes its decoding.
            request_body.pop("response_format")
            _body(dict(extraction_template))  # Validate finite, bounded JSON first.
            request_body["chat_template_kwargs"] = {
                "template": json.dumps(dict(extraction_template), separators=(",", ":")),
                "instructions": extraction_instructions,
                "enable_thinking": False,
            }
        elif extraction_template is not None or extraction_instructions is not None:
            raise InferenceTransportError(
                "this model does not support the NuExtract extraction template",
                retryable=False,
            )
        from .config import OpenRouterGenerationConfig
        if isinstance(self.config, OpenRouterGenerationConfig):
            request_body["provider"] = {"data_collection": "deny", "require_parameters": True}
            # These bounded classification/extraction calls need a JSON answer.
            # Otherwise a thinking model can spend the entire output allowance
            # on reasoning and return no content to validate.
            request_body["reasoning"] = {"enabled": False}
        encoded_request = _body(request_body)
        # Count the complete canonical request, including roles, schema and fixed
        # envelope, at the deliberately strict rate of one UTF-8 byte per possible
        # token. The reserve covers worker/chat-template control tokens not present in
        # the OpenAI JSON body.
        if (
            len(encoded_request)
            + max_output_tokens
            + INFERENCE_REQUEST_TOKEN_RESERVE
            > self.max_input_tokens
        ):
            raise InferenceTransportError(
                "inference request exceeds the configured context limit",
                retryable=False,
            )
        try:
            payload = self._transport(
                self.config.base_url + "/chat/completions",
                {
                    "Authorization": "Bearer "
                    + load_credential(self.config.credential_file),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "job-search-inference/1.0",
                },
                encoded_request,
                self.config.timeout_seconds,
                self.config.max_response_bytes,
            )
        except InferenceTransportError as exc:
            if isinstance(exc, UsageDeferred):
                raise
            if isinstance(self.config, OpenAIGenerationConfig) and exc.retryable:
                raise InferenceTransportError(
                    "synchronous inference submission outcome is ambiguous; inspect "
                    "the provider before a manual retry",
                    retryable=False,
                    status_code=exc.status_code,
                ) from None
            raise
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise InferenceResponseRejected(
                "generation response must contain exactly one choice",
                retryable=False,
            )
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise InferenceResponseRejected(
                "generation response did not contain text",
                retryable=False,
            )
        response_model = payload.get("model")
        if (
            not isinstance(response_model, str)
            or not response_model
            or len(response_model) > 255
            or response_model != self.config.model
        ):
            raise InferenceResponseRejected(
                "generation response must attest the exact configured model",
                retryable=False,
            )
        response_usage = _usage(payload)
        response_usage["finish_reason"] = choice.get("finish_reason")
        response_usage["response_id"] = str(payload.get("id") or "")
        response_usage["response_model"] = str(response_model or "")
        if isinstance(self.config, OpenRouterGenerationConfig):
            response_usage["response_provider"] = str(payload.get("provider") or "")[:128]
        return GenerationResult(content, response_usage, dict(self.provenance))


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        config: OpenAIEmbeddingConfig,
        transport: Transport = post_json,
    ) -> None:
        self.config = config
        self._transport = _managed_sync_transport(transport, config.embedding_identity, "embeddings") if isinstance(config, OpenAIEmbeddingConfig) else transport
        # The downstream cache/artifact key must change with endpoint/provider or
        # deployment identity, not just with the declared weights revision.
        self.model_revision = config.embedding_identity

    @property
    def provenance(self) -> Mapping[str, Any]:
        return self.config.provenance

    @staticmethod
    def _input_upper_bound(text: str) -> int:
        return len(text.encode("utf-8"))

    def _batches(self, texts: Sequence[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for text in texts:
            if not isinstance(text, str):
                raise InferenceTransportError(
                    "embedding inputs must be strings", retryable=False
                )
            estimate = self._input_upper_bound(text)
            if estimate > self.config.max_input_tokens:
                raise InferenceTransportError(
                    "one embedding input exceeds the configured token limit",
                    retryable=False,
                )
            if current and (
                len(current) >= self.config.max_batch_size
                or current_tokens + estimate > self.config.max_input_tokens
            ):
                batches.append(current)
                current = []
                current_tokens = 0
            current.append(text)
            current_tokens += estimate
        if current:
            batches.append(current)
        return batches

    def _validate_response_identity(self, payload: Mapping[str, Any]) -> None:
        """Validate provider-specific identity evidence when the protocol has it."""

        del payload

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector for _batch, vectors in self.iter_encode_batches(texts) for vector in vectors]

    def iter_encode_batches(self, texts: Sequence[str]):
        """Yield fully validated batches so cache owners can checkpoint progress."""
        for batch in self._batches(texts):
            try:
                payload = self._transport(
                    self.config.base_url + "/embeddings",
                    {
                        "Authorization": "Bearer "
                        + load_credential(self.config.credential_file),
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": "job-search-inference/1.0",
                    },
                    _body(
                        {
                            "model": self.config.model,
                            "input": batch,
                            "encoding_format": "float",
                        }
                    ),
                    self.config.timeout_seconds,
                    self.config.max_response_bytes,
                )
            except InferenceTransportError as exc:
                if isinstance(exc, UsageDeferred):
                    raise
                if isinstance(self.config, OpenAIEmbeddingConfig) and exc.retryable:
                    raise InferenceTransportError(
                        "synchronous inference submission outcome is ambiguous; inspect "
                        "the provider before a manual retry",
                        retryable=False,
                        status_code=exc.status_code,
                    ) from None
                raise
            data = payload.get("data")
            response_model = payload.get("model")
            if (
                not isinstance(response_model, str)
                or not response_model
                or len(response_model) > 255
                or response_model != self.config.model
            ):
                raise InferenceTransportError(
                    "embedding response must attest the exact configured model",
                    retryable=False,
                )
            self._validate_response_identity(payload)
            if not isinstance(data, list) or len(data) != len(batch):
                raise InferenceTransportError(
                    "embedding response returned the wrong number of vectors",
                    retryable=False,
                )
            indexed: dict[int, list[float]] = {}
            for item in data:
                if not isinstance(item, Mapping):
                    raise InferenceTransportError(
                        "embedding response item is not an object",
                        retryable=False,
                    )
                index = item.get("index")
                vector = item.get("embedding")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or index in indexed
                    or not isinstance(vector, list)
                    or len(vector) != self.config.dimensions
                ):
                    raise InferenceTransportError(
                        "embedding response violates index or dimension contract",
                        retryable=False,
                    )
                converted: list[float] = []
                for value in vector:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise InferenceTransportError(
                            "embedding vector contains a non-number",
                            retryable=False,
                        )
                    number = float(value)
                    if not math.isfinite(number):
                        raise InferenceTransportError(
                            "embedding vector contains a non-finite number",
                            retryable=False,
                        )
                    converted.append(number)
                indexed[index] = converted
            if set(indexed) != set(range(len(batch))):
                raise InferenceTransportError(
                    "embedding response indexes are incomplete",
                    retryable=False,
                )
            yield batch, [indexed[index] for index in range(len(batch))]


class RunpodQueuedStructuredGenerator(OpenAICompatibleStructuredGenerator):
    """OpenAI-compatible generation submitted through Runpod's durable queue."""

    def __init__(
        self,
        config: RunpodQueuedGenerationConfig,
        *,
        transport: RunpodTransport = runpod_request_json,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        queued = _RunpodQueuedOpenAITransport(
            config,
            "/chat/completions",
            transport=transport,
            clock=clock,
            sleeper=sleeper,
        )
        self._queued_transport = queued
        super().__init__(config, queued)  # type: ignore[arg-type]


class RunpodQueuedEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    """OpenAI-compatible embeddings submitted through Runpod's durable queue."""

    def __init__(
        self,
        config: RunpodQueuedEmbeddingConfig,
        *,
        transport: RunpodTransport = runpod_request_json,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        queued = _RunpodQueuedOpenAITransport(
            config,
            "/embeddings",
            transport=transport,
            clock=clock,
            sleeper=sleeper,
        )
        self._queued_transport = queued
        super().__init__(config, queued)  # type: ignore[arg-type]

    def _validate_response_identity(self, payload: Mapping[str, Any]) -> None:
        expected_revision = self.config.model_revision.rsplit("@", 1)[1]
        if (
            payload.get("job_search_model_revision") != expected_revision
            or payload.get("job_search_worker_protocol")
            != RUNPOD_INFINITY_EMBEDDING_PROTOCOL
        ):
            raise InferenceTransportError(
                "Runpod embedding worker did not attest the configured model revision",
                retryable=False,
            )
