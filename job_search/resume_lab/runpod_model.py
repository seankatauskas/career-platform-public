"""Runpod Serverless transport for the resume lab's validated JSON tasks.

Only inference crosses this boundary.  Task construction and every semantic output
validator remain in :mod:`job_search.resume_lab.model`, so a remote worker has no
authority over scores, grounding, provenance, or artifact selection.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from job_search.contracts import ContractError, validate_identifier, canonical_json

from .local_model_driver import (
    DriverError,
    PromptBundle,
    build_prompt,
    extract_json_object,
)
from .model import (
    LocalJsonResumeModel,
    ResumeModelError,
    build_resume_model_request,
)

RUNPOD_MODEL_CONFIG_VERSION = 2
RUNPOD_PROVIDER = "runpod_serverless_vllm"
RUNPOD_API_ORIGIN = "https://api.runpod.ai"
MAX_RUNPOD_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RUNPOD_REQUEST_BYTES = 2 * 1024 * 1024
MAX_API_KEY_BYTES = 1024
# worker-vLLM applies the selected model's chat template after receiving the two
# message contents.  The exact tokenizer is deliberately not a local dependency,
# so reserve ample fixed space for its role/control tokens and count every UTF-8
# byte as a possible token.  This is intentionally much stricter than the usual
# bytes-per-token estimate and prevents an oversized request from consuming a
# remote job only to fail at the model context boundary.
RUNPOD_CHAT_TEMPLATE_TOKEN_RESERVE = 512

_ENDPOINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
_MODEL_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PENDING_STATUSES = frozenset({"IN_QUEUE", "IN_PROGRESS"})
_TERMINAL_FAILURE_STATUSES = frozenset({"FAILED", "CANCELLED", "TIMED_OUT"})
_AMBIGUOUS_SUBMISSION_STATUSES = frozenset({408, 409, 425, 429})

HttpTransport = Callable[
    [str, str, Mapping[str, str], Optional[bytes], float],
    tuple[int, Mapping[str, str], bytes],
]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Make redirect responses visible to the caller instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _RunpodHttpStatusError(ResumeModelError):
    def __init__(self, status_code: int, retry_after: float = 0.0) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"Runpod resume model returned HTTP {status_code}")


class _RunpodTransportError(ResumeModelError):
    pass


class RunpodReconciliationRequired(ResumeModelError):
    """A queued job may still exist, so another submission needs owner review."""

    def __init__(self, job_id: str = "") -> None:
        self.job_id = job_id
        detail = f" for job {job_id}" if job_id else ""
        super().__init__(
            "Runpod resume submission outcome requires manual reconciliation"
            f"{detail} before retry"
        )


def _validate_runpod_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.runpod.ai"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/v2/")
    ):
        raise ResumeModelError("Runpod resume model URL is invalid")


def _read_bounded_response(response: Any) -> tuple[Mapping[str, str], bytes]:
    headers = {
        str(name).lower(): str(value) for name, value in response.headers.items()
    }
    raw_length = headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError:
            raise _RunpodTransportError(
                "Runpod resume model returned invalid headers"
            ) from None
        if length < 0 or length > MAX_RUNPOD_RESPONSE_BYTES:
            raise _RunpodTransportError("Runpod resume model response is too large")
    body = response.read(MAX_RUNPOD_RESPONSE_BYTES + 1)
    if not isinstance(body, bytes) or len(body) > MAX_RUNPOD_RESPONSE_BYTES:
        raise _RunpodTransportError("Runpod resume model response is too large")
    return headers, body


def default_runpod_http_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, Mapping[str, str], bytes]:
    """Execute one fixed-origin request with redirects and large bodies disabled."""

    _validate_runpod_url(url)
    if method not in {"GET", "POST"}:
        raise ResumeModelError("Runpod resume model method is invalid")
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method=method,
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    response: Any
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, TimeoutError, urllib.error.URLError):
        raise _RunpodTransportError(
            "Runpod resume model transport failed safely"
        ) from None
    try:
        final_url = response.geturl()
        if final_url != url:
            raise _RunpodTransportError(
                "Runpod resume model refused a redirected response"
            )
        response_headers, response_body = _read_bounded_response(response)
        status_code = int(response.getcode())
    except _RunpodTransportError:
        raise
    except (
        http.client.HTTPException,
        OSError,
        TimeoutError,
        TypeError,
        ValueError,
    ):
        raise _RunpodTransportError(
            "Runpod resume model transport failed safely"
        ) from None
    finally:
        try:
            response.close()
        except (http.client.HTTPException, OSError):
            raise _RunpodTransportError(
                "Runpod resume model transport failed safely"
            ) from None
    return status_code, response_headers, response_body


def _safe_api_key_descriptor(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ResumeModelError("Runpod API key file is unavailable or unsafe") from None
    try:
        info = os.fstat(descriptor)
        current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != current_uid
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ResumeModelError(
                "Runpod API key file must be an owner-only regular file"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _api_key_file_is_usable(path: Path) -> bool:
    try:
        _read_api_key(path)
    except ResumeModelError:
        return False
    return True


def _read_api_key(path: Path) -> str:
    descriptor = _safe_api_key_descriptor(path)
    try:
        encoded = os.read(descriptor, MAX_API_KEY_BYTES + 1)
    finally:
        os.close(descriptor)
    if not encoded or len(encoded) > MAX_API_KEY_BYTES:
        raise ResumeModelError("Runpod API key file has invalid content")
    try:
        decoded = encoded.decode("ascii")
    except UnicodeDecodeError:
        raise ResumeModelError("Runpod API key file has invalid content") from None
    key = decoded.rstrip("\r\n")
    if (
        decoded not in {key, key + "\n", key + "\r\n"}
        or not 16 <= len(key) <= MAX_API_KEY_BYTES
        or any(
            character.isspace() or ord(character) < 33 or ord(character) > 126
            for character in key
        )
    ):
        raise ResumeModelError("Runpod API key file has invalid content")
    return key


def _finite_number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ContractError(f"{field} is outside the supported range")
    return normalized


@dataclass(frozen=True)
class RunpodResumeModelConfig:
    """Attested identity and bounded runtime controls for worker-vLLM."""

    producer_version: str
    endpoint_id: str
    model_id: str
    model_revision: str
    worker_image_digest: str
    api_key_file: Path
    request_timeout_seconds: float = 30.0
    timeout_seconds: float = 900.0
    poll_interval_seconds: float = 1.0
    context_size: int = 32_768
    max_tokens: int = 8192
    temperature: float = 0.1
    top_p: float = 0.95
    version: int = RUNPOD_MODEL_CONFIG_VERSION
    provider: str = RUNPOD_PROVIDER

    def __post_init__(self) -> None:
        try:
            validate_identifier(self.producer_version, "producer_version")
        except (TypeError, ValueError) as exc:
            raise ContractError(str(exc)) from exc
        if self.version != RUNPOD_MODEL_CONFIG_VERSION:
            raise ContractError("Runpod resume model config version must be 2")
        if self.provider != RUNPOD_PROVIDER:
            raise ContractError("resume model provider is unsupported")
        if not isinstance(self.endpoint_id, str) or not _ENDPOINT_ID.fullmatch(
            self.endpoint_id
        ):
            raise ContractError("Runpod endpoint_id is invalid")
        if (
            not isinstance(self.model_id, str)
            or not _MODEL_ID.fullmatch(self.model_id)
            or "//" in self.model_id
            or ".." in self.model_id.split("/")
            or self.model_id.endswith("/")
        ):
            raise ContractError("Runpod model_id is invalid")
        if not isinstance(self.model_revision, str) or not _MODEL_REVISION.fullmatch(
            self.model_revision
        ):
            raise ContractError(
                "Runpod model_revision must be an immutable 40- or 64-character commit"
            )
        if not isinstance(self.worker_image_digest, str) or not _IMAGE_DIGEST.fullmatch(
            self.worker_image_digest
        ):
            raise ContractError(
                "Runpod worker_image_digest must be an immutable SHA-256 image digest"
            )
        try:
            path = Path(self.api_key_file).expanduser()
        except TypeError as exc:
            raise ContractError("Runpod api_key_file must be absolute") from exc
        if not path.is_absolute():
            raise ContractError("Runpod api_key_file must be absolute")
        object.__setattr__(self, "api_key_file", path)
        request_timeout = _finite_number(
            self.request_timeout_seconds,
            "request_timeout_seconds",
            1,
            60,
        )
        overall_timeout = _finite_number(
            self.timeout_seconds, "timeout_seconds", 10, 1800
        )
        poll_interval = _finite_number(
            self.poll_interval_seconds, "poll_interval_seconds", 0.05, 10
        )
        if request_timeout > overall_timeout:
            raise ContractError("request_timeout_seconds cannot exceed timeout_seconds")
        object.__setattr__(self, "request_timeout_seconds", request_timeout)
        object.__setattr__(self, "timeout_seconds", overall_timeout)
        object.__setattr__(self, "poll_interval_seconds", poll_interval)
        if (
            isinstance(self.context_size, bool)
            or not isinstance(self.context_size, int)
            or not 2_048 <= self.context_size <= 262_144
        ):
            raise ContractError("Runpod context_size must be between 2048 and 262144")
        if (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or not 64 <= self.max_tokens <= 32_768
        ):
            raise ContractError("Runpod max_tokens must be between 64 and 32768")
        if self.max_tokens >= self.context_size:
            raise ContractError("Runpod max_tokens must be smaller than context_size")
        object.__setattr__(
            self,
            "temperature",
            _finite_number(self.temperature, "temperature", 0, 2),
        )
        object.__setattr__(
            self, "top_p", _finite_number(self.top_p, "top_p", 0.000001, 1)
        )

    @property
    def model_sha256(self) -> None:
        """Compatibility field: remote model identity is a revision, not a file hash."""

        return None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RunpodResumeModelConfig:
        expected = {
            "version",
            "provider",
            "producer_version",
            "endpoint_id",
            "model_id",
            "model_revision",
            "worker_image_digest",
            "api_key_file",
            "request_timeout_seconds",
            "timeout_seconds",
            "poll_interval_seconds",
            "context_size",
            "max_tokens",
            "temperature",
            "top_p",
        }
        if set(raw) != expected:
            raise ContractError(
                "resume model config fields do not match Runpod version 2"
            )
        key_path = raw["api_key_file"]
        if not isinstance(key_path, str) or not key_path or len(key_path) > 4096:
            raise ContractError("Runpod api_key_file must be an absolute path")
        return cls(
            producer_version=raw["producer_version"],
            endpoint_id=raw["endpoint_id"],
            model_id=raw["model_id"],
            model_revision=raw["model_revision"],
            worker_image_digest=raw["worker_image_digest"],
            api_key_file=Path(key_path),
            request_timeout_seconds=raw["request_timeout_seconds"],
            timeout_seconds=raw["timeout_seconds"],
            poll_interval_seconds=raw["poll_interval_seconds"],
            context_size=raw["context_size"],
            max_tokens=raw["max_tokens"],
            temperature=raw["temperature"],
            top_p=raw["top_p"],
            version=raw["version"],
            provider=raw["provider"],
        )

    def build(self) -> RunpodJsonResumeModel:
        return RunpodJsonResumeModel(self)

    def provenance(self) -> Mapping[str, Any]:
        """Return immutable, credential-free identity for persisted artifacts."""

        return {
            "provider": self.provider,
            "producer_version": self.producer_version,
            "config_version": self.version,
            "endpoint_fingerprint": hashlib.sha256(
                self.endpoint_id.encode("ascii")
            ).hexdigest(),
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "worker_image_digest": self.worker_image_digest,
            "context_size": self.context_size,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }


def runpod_resume_model_status(
    config: RunpodResumeModelConfig,
) -> Mapping[str, Any]:
    """Return credential metadata readiness without making a network request."""

    if not isinstance(config, RunpodResumeModelConfig):
        raise ContractError("Runpod resume model config is invalid")
    credential_available = _api_key_file_is_usable(config.api_key_file)
    return {
        "status": "configuration_ready" if credential_available else "blocked_setup",
        "reason": None
        if credential_available
        else "api_key_file_unavailable_or_unsafe",
        "provider": config.provider,
        "backend": "worker-vllm",
        "config_version": config.version,
        "producer_version": config.producer_version,
        "model_sha256": None,
        "model_id": config.model_id,
        "model_revision": config.model_revision,
        "worker_image_digest": config.worker_image_digest,
        "context_size": config.context_size,
        "endpoint_fingerprint": config.provenance()["endpoint_fingerprint"],
        "credential_available": credential_available,
        "external_endpoint_probed": False,
        "command_available": True,
        "isolation_available": True,
        "missing_read_paths": 0,
    }


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise ContractError("Runpod resume model request must be finite JSON") from exc
    if len(encoded) > MAX_RUNPOD_REQUEST_BYTES:
        raise ContractError("Runpod resume model request is too large")
    return encoded


def _prompt_token_upper_bound(prompt: PromptBundle) -> int:
    """Return a tokenizer-independent upper bound for a two-message prompt."""

    try:
        content_bytes = len(prompt.system.encode("utf-8")) + len(
            prompt.user.encode("utf-8")
        )
    except UnicodeEncodeError:
        raise ResumeModelError("Runpod resume model prompt is invalid") from None
    return content_bytes + RUNPOD_CHAT_TEMPLATE_TOKEN_RESERVE


def _assert_prompt_fits_context(
    prompt: PromptBundle,
    *,
    context_size: int,
    max_tokens: int,
) -> None:
    if _prompt_token_upper_bound(prompt) + max_tokens > context_size:
        raise ResumeModelError(
            "Runpod resume model prompt exceeds the configured context size"
        )


def _reject_nonfinite(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _json_mapping(body: bytes) -> Mapping[str, Any]:
    if not isinstance(body, bytes) or len(body) > MAX_RUNPOD_RESPONSE_BYTES:
        raise ResumeModelError("Runpod resume model response is too large")
    try:
        value = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ResumeModelError("Runpod resume model returned invalid JSON") from None
    if not isinstance(value, Mapping):
        raise ResumeModelError("Runpod resume model response must be an object")
    return value


def _retry_after(headers: Mapping[str, str]) -> float:
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None or not re.fullmatch(r"[0-9]{1,3}", value):
        return 0.0
    return min(float(value), 10.0)


def _openai_completion_content(output: Any, expected_model: str) -> str:
    """Unwrap worker-vLLM's direct or generic-proxy response body."""

    value = output
    # The first-party worker uses ``return_aggregate_stream=True`` and its
    # non-streaming handler yields exactly one parsed vLLM response.
    if isinstance(value, list):
        if len(value) != 1:
            raise ResumeModelError("Runpod worker-vLLM returned an invalid completion")
        value = value[0]
    if isinstance(value, Mapping) and "body" in value:
        status_code = value.get("status_code", value.get("statusCode", 200))
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 200 <= status_code < 300
        ):
            raise ResumeModelError("Runpod worker-vLLM proxy request failed")
        value = value["body"]
        if isinstance(value, str):
            try:
                encoded_value = value.encode("utf-8")
            except UnicodeEncodeError:
                raise ResumeModelError(
                    "Runpod worker-vLLM returned an invalid completion"
                ) from None
            if len(encoded_value) > MAX_RUNPOD_RESPONSE_BYTES:
                raise ResumeModelError("Runpod worker-vLLM output is too large")
            value = _json_mapping(encoded_value)
    try:
        if value["model"] != expected_model:
            raise TypeError
        choices = value["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise TypeError
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise TypeError
        if choice.get("finish_reason") == "length":
            raise ResumeModelError(
                "Runpod worker-vLLM exhausted the configured output limit"
            )
        message = choice["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError):
        raise ResumeModelError(
            "Runpod worker-vLLM returned an invalid completion"
        ) from None
    if not isinstance(content, str) or not content.strip():
        raise ResumeModelError("Runpod worker-vLLM returned an invalid completion")
    try:
        content_size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        raise ResumeModelError(
            "Runpod worker-vLLM returned an invalid completion"
        ) from None
    if content_size > MAX_RUNPOD_RESPONSE_BYTES:
        raise ResumeModelError("Runpod worker-vLLM returned an invalid completion")
    return content


class RunpodJsonResumeModel(LocalJsonResumeModel):
    """Use worker-vLLM remotely while inheriting all local task validators."""

    def __init__(
        self,
        config: RunpodResumeModelConfig,
        *,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(config, RunpodResumeModelConfig):
            raise ContractError("Runpod resume model config is invalid")
        self.config = config
        self._api_key = _read_api_key(config.api_key_file)
        self._transport = transport or default_runpod_http_transport
        self._clock = clock
        self._sleep = sleeper

    def provenance(self) -> Mapping[str, Any]:
        return self.config.provenance()

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None,
        timeout: float,
    ) -> Mapping[str, Any]:
        from ..inference.usage import heartbeat_scope
        heartbeat_scope()
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ResumeModelError("Runpod resume model path is invalid")
        url = f"{RUNPOD_API_ORIGIN}{path}"
        _validate_runpod_url(url)
        encoded = _canonical_bytes(body) if body is not None else None
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "job-search-resume-lab/1",
        }
        try:
            result = self._transport(method, url, headers, encoded, timeout)
        except ResumeModelError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError):
            raise _RunpodTransportError(
                "Runpod resume model transport failed safely"
            ) from None
        if not isinstance(result, tuple) or len(result) != 3:
            raise _RunpodTransportError(
                "Runpod resume model transport returned an invalid response"
            )
        status_code, response_headers, response_body = result
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
            or not isinstance(response_headers, Mapping)
            or not isinstance(response_body, bytes)
        ):
            raise _RunpodTransportError(
                "Runpod resume model transport returned an invalid response"
            )
        normalized_headers = {
            str(name).lower(): str(value) for name, value in response_headers.items()
        }
        if len(response_body) > MAX_RUNPOD_RESPONSE_BYTES:
            raise ResumeModelError("Runpod resume model response is too large")
        content_type = normalized_headers.get("content-type")
        if content_type is not None and not content_type.lower().startswith(
            "application/json"
        ):
            raise ResumeModelError("Runpod resume model response is not JSON")
        if not 200 <= status_code < 300:
            raise _RunpodHttpStatusError(status_code, _retry_after(normalized_headers))
        return _json_mapping(response_body)

    @staticmethod
    def _validate_job_id(value: Any) -> str:
        if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
            raise ResumeModelError("Runpod returned an invalid job id")
        return value

    def _wait(self, seconds: float, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise ResumeModelError("Runpod resume model timed out")
        self._sleep(min(seconds, remaining))

    def _poll_job(self, first: Mapping[str, Any], job_id: str, deadline: float, invocation: Any = None) -> Any:
        envelope = first
        next_delay = self.config.poll_interval_seconds
        while True:
            try:
                reported_id = self._validate_job_id(envelope.get("id"))
            except ResumeModelError:
                if invocation is not None:
                    invocation.unavailable()
                raise RunpodReconciliationRequired(job_id) from None
            if reported_id != job_id:
                if invocation is not None:
                    invocation.unavailable()
                raise RunpodReconciliationRequired(job_id)
            status_value = envelope.get("status", "IN_QUEUE")
            if not isinstance(status_value, str):
                if invocation is not None:
                    invocation.unavailable()
                raise RunpodReconciliationRequired(job_id)
            status = status_value.upper()
            if status == "COMPLETED":
                if invocation is not None:
                    invocation.terminal("completed")
                if "output" not in envelope:
                    raise ResumeModelError("Runpod completed without model output")
                return envelope["output"]
            if status in _TERMINAL_FAILURE_STATUSES:
                if invocation is not None:
                    invocation.terminal("cancelled" if status == "CANCELLED" else "failed")
                raise ResumeModelError(
                    f"Runpod resume model job ended with status {status}"
                )
            if status not in _PENDING_STATUSES:
                if invocation is not None:
                    invocation.unavailable()
                raise RunpodReconciliationRequired(job_id)
            try:
                self._wait(next_delay, deadline)
            except ResumeModelError:
                raise RunpodReconciliationRequired(job_id) from None
            next_delay = self.config.poll_interval_seconds
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise RunpodReconciliationRequired(job_id)
            try:
                envelope = self._request_json(
                    "GET",
                    f"/v2/{self.config.endpoint_id}/status/{job_id}",
                    body=None,
                    timeout=min(self.config.request_timeout_seconds, remaining),
                )
            except _RunpodHttpStatusError as exc:
                if exc.status_code == 429 or 500 <= exc.status_code <= 599:
                    next_delay = max(self.config.poll_interval_seconds, exc.retry_after)
                    continue
                if invocation is not None:
                    invocation.unavailable()
                raise RunpodReconciliationRequired(job_id) from None
            except _RunpodTransportError:
                # A GET is safe to repeat.  Submission POSTs are deliberately never
                # retried because an ambiguous failure could duplicate GPU spend.
                continue
            except ResumeModelError:
                # Bad content type, malformed JSON, or an otherwise invalid status
                # response cannot establish a terminal state for the accepted job.
                if invocation is not None:
                    invocation.unavailable()
                raise RunpodReconciliationRequired(job_id) from None

    def _invoke(
        self,
        task: str,
        payload: Mapping[str, Any],
        output_schema: Mapping[str, Any],
        *,
        generation_seed: int | None = None,
    ) -> Mapping[str, Any]:
        request = build_resume_model_request(
            task,
            payload,
            output_schema,
            generation_seed=generation_seed,
        )
        try:
            prompt = build_prompt(request)
        except DriverError as exc:
            raise ResumeModelError("resume model request contract is invalid") from exc
        _assert_prompt_fits_context(
            prompt,
            context_size=self.config.context_size,
            max_tokens=self.config.max_tokens,
        )
        seed = generation_seed if generation_seed is not None else 0
        run_request = {
            "input": {
                "route": "/v1/chat/completions",
                "method": "POST",
                "body": {
                    "model": self.config.model_id,
                    "messages": prompt.messages,
                    "max_tokens": self.config.max_tokens,
                    "temperature": self.config.temperature,
                    "top_p": self.config.top_p,
                    "seed": seed,
                    "stream": False,
                },
            },
            "policy": {
                "executionTimeout": int(self.config.timeout_seconds * 1000),
                "ttl": int(min(604_800, self.config.timeout_seconds + 300) * 1000),
            },
        }
        deadline = self._clock() + self.config.timeout_seconds
        from ..inference.usage import begin_invocation, InvocationPending
        request_bytes = _canonical_bytes(run_request)
        invocation = begin_invocation(canonical_json(self.config.provenance()), "resume_generation", request_bytes,
                                      reserved_tokens=len(request_bytes) + self.config.max_tokens + 512, retrieval_kind="runpod_job")
        if invocation is not None and invocation.job_id:
            try:
                first = self._request_json("GET", f"/v2/{self.config.endpoint_id}/status/{invocation.job_id}",
                                           body=None, timeout=self.config.request_timeout_seconds)
                output = self._poll_job(first, invocation.job_id, deadline, invocation)
            except RunpodReconciliationRequired:
                raise InvocationPending() from None
            except _RunpodHttpStatusError as exc:
                if exc.status_code != 429 and not 500 <= exc.status_code <= 599:
                    invocation.unavailable()
                raise InvocationPending() from None
            except _RunpodTransportError:
                raise InvocationPending() from None
            raw = _openai_completion_content(output, self.config.model_id)
            try:
                return extract_json_object(raw)
            except DriverError:
                raise ResumeModelError("Runpod resume model returned invalid task JSON") from None
        if invocation is not None:
            invocation.submitting()
        try:
            first = self._request_json(
                "POST",
                f"/v2/{self.config.endpoint_id}/run",
                body=run_request,
                timeout=min(
                    self.config.request_timeout_seconds,
                    self.config.timeout_seconds,
                ),
            )
        except _RunpodHttpStatusError as exc:
            if (
                exc.status_code in _AMBIGUOUS_SUBMISSION_STATUSES
                or 500 <= exc.status_code <= 599
            ):
                if invocation is not None:
                    invocation.unknown()
                raise RunpodReconciliationRequired() from None
            if invocation is not None:
                invocation.terminal("failed")
            raise
        except _RunpodTransportError:
            # The service may have accepted the POST before the response was lost.
            # Retrying here or through the dashboard could duplicate GPU work.
            if invocation is not None:
                invocation.unknown()
            raise RunpodReconciliationRequired() from None
        except ResumeModelError:
            # A malformed 2xx response is equally ambiguous: it may have contained an
            # accepted job whose identifier could not be trusted.
            if invocation is not None:
                invocation.unknown()
            raise RunpodReconciliationRequired() from None
        try:
            job_id = self._validate_job_id(first.get("id"))
        except ResumeModelError:
            if invocation is not None:
                invocation.unknown()
            raise RunpodReconciliationRequired() from None
        if invocation is not None:
            invocation.accepted(job_id)
        try:
            output = self._poll_job(first, job_id, deadline, invocation)
        except RunpodReconciliationRequired:
            if invocation is not None:
                raise InvocationPending() from None
            raise
        raw = _openai_completion_content(output, self.config.model_id)
        try:
            return extract_json_object(raw)
        except DriverError:
            raise ResumeModelError(
                "Runpod resume model returned invalid task JSON"
            ) from None
