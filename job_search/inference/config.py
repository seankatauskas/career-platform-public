"""Strict owner-only configuration for portable inference providers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import InferenceConfigError

INFERENCE_CONFIG_VERSION = 1
INFERENCE_CONFIG_ENV = "JOB_SEARCH_INFERENCE_CONFIG"
MAX_CONFIG_BYTES = 64 * 1024
MAX_CREDENTIAL_BYTES = 16 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_RUNPOD_ENDPOINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
_HF_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
RUNPOD_API_ORIGIN = "https://api.runpod.ai"
RUNPOD_VLLM_PROXY_PROTOCOL = "worker_vllm_proxy_v1"
RUNPOD_INFINITY_EMBEDDING_PROTOCOL = "job_search_infinity_exact_v1"


@dataclass(frozen=True)
class OpenAIGenerationConfig:
    provider_id: str
    base_url: str
    model: str
    model_revision: str
    deployment_revision: str
    credential_file: Path
    timeout_seconds: float
    max_response_bytes: int
    max_input_tokens: int
    default_max_output_tokens: int
    json_schema_mode: bool

    @property
    def generation_identity(self) -> str:
        return _provider_identity(
            self.model_revision,
            {
                "provider": self.provider_id,
                "protocol": "openai-compatible-chat-completions",
                "endpoint": self.base_url,
                "model": self.model,
                "weights_revision": self.model_revision,
                "deployment_revision": self.deployment_revision,
                "json_schema_mode": self.json_schema_mode,
                "default_max_output_tokens": self.default_max_output_tokens,
            },
        )

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "protocol": "openai-compatible-chat-completions",
            "endpoint": self.base_url,
            "model": self.model,
            "model_revision": self.model_revision,
            "weights_revision": self.model_revision,
            "deployment_revision": self.deployment_revision,
            "generation_identity": self.generation_identity,
        }


@dataclass(frozen=True)
class OpenRouterGenerationConfig(OpenAIGenerationConfig):
    """Hosted model identity; no claim of immutable provider weights."""

    @property
    def generation_identity(self) -> str:
        return _provider_identity(self.model, {
            "provider": "openrouter", "model": self.model,
            "endpoint": self.base_url, "identity_kind": "hosted_model_id",
            "data_collection": "deny", "require_parameters": True,
            "reasoning_enabled": False, "json_schema_mode": True,
            "default_max_output_tokens": self.default_max_output_tokens,
        })

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "provider": "openrouter", "protocol": "openai-compatible-chat-completions",
            "endpoint": self.base_url, "model": self.model,
            "model_revision": self.model, "weights_revision": None,
            "deployment_revision": None, "identity_kind": "hosted_model_id",
            "generation_identity": self.generation_identity,
            "data_collection": "deny", "require_parameters": True,
            "reasoning_enabled": False,
        }


@dataclass(frozen=True)
class OpenAIEmbeddingConfig:
    provider_id: str
    base_url: str
    model: str
    model_revision: str
    deployment_revision: str
    credential_file: Path
    timeout_seconds: float
    max_response_bytes: int
    max_input_tokens: int
    max_batch_size: int
    dimensions: int

    @property
    def embedding_identity(self) -> str:
        return _embedding_identity(
            self.model_revision,
            {
                "provider": self.provider_id,
                "protocol": "openai-compatible-embeddings",
                "endpoint": self.base_url,
                "model": self.model,
                "weights_revision": self.model_revision,
                "deployment_revision": self.deployment_revision,
                "dimensions": self.dimensions,
            },
        )

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "protocol": "openai-compatible-embeddings",
            "endpoint": self.base_url,
            "model": self.model,
            "model_revision": self.embedding_identity,
            "weights_revision": self.model_revision,
            "deployment_revision": self.deployment_revision,
            "dimensions": self.dimensions,
        }


def _provider_identity(weights_revision: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"{weights_revision}#provider-{hashlib.sha256(encoded).hexdigest()}"


def _embedding_identity(weights_revision: str, value: Mapping[str, Any]) -> str:
    return _provider_identity(weights_revision, value)


def _runpod_endpoint_fingerprint(endpoint_id: str) -> str:
    return hashlib.sha256(endpoint_id.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class RunpodQueuedGenerationConfig:
    """Pinned worker identity and bounded controls for queued chat generation."""

    provider_id: str
    endpoint_id: str
    worker_protocol: str
    model: str
    model_revision: str
    deployment_revision: str
    credential_file: Path
    request_timeout_seconds: float
    job_timeout_seconds: float
    poll_interval_seconds: float
    max_response_bytes: int
    max_input_tokens: int
    default_max_output_tokens: int
    json_schema_mode: bool

    @property
    def base_url(self) -> str:
        # This compatibility property lets the queue provider reuse the ordinary
        # OpenAI request/response validators without making the origin configurable.
        return f"{RUNPOD_API_ORIGIN}/v2/{self.endpoint_id}/openai/v1"

    @property
    def timeout_seconds(self) -> float:
        return self.job_timeout_seconds

    @property
    def generation_identity(self) -> str:
        return _provider_identity(
            self.model_revision,
            {
                "provider": self.provider_id,
                "protocol": "runpod-queue+openai-compatible-chat-completions",
                "worker_protocol": self.worker_protocol,
                "endpoint_fingerprint": _runpod_endpoint_fingerprint(self.endpoint_id),
                "model": self.model,
                "weights_revision": self.model_revision,
                "deployment_revision": self.deployment_revision,
                "json_schema_mode": self.json_schema_mode,
                "default_max_output_tokens": self.default_max_output_tokens,
            },
        )

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "protocol": "runpod-queue+openai-compatible-chat-completions",
            "worker_protocol": self.worker_protocol,
            "endpoint_origin": RUNPOD_API_ORIGIN,
            "endpoint_fingerprint": _runpod_endpoint_fingerprint(self.endpoint_id),
            "model": self.model,
            "model_revision": self.model_revision,
            "weights_revision": self.model_revision,
            "deployment_revision": self.deployment_revision,
            "generation_identity": self.generation_identity,
        }


@dataclass(frozen=True)
class RunpodQueuedEmbeddingConfig:
    """Pinned worker identity and bounded controls for queued embeddings."""

    provider_id: str
    endpoint_id: str
    worker_protocol: str
    model: str
    model_revision: str
    deployment_revision: str
    credential_file: Path
    request_timeout_seconds: float
    job_timeout_seconds: float
    poll_interval_seconds: float
    max_response_bytes: int
    max_input_tokens: int
    max_batch_size: int
    dimensions: int

    @property
    def base_url(self) -> str:
        return f"{RUNPOD_API_ORIGIN}/v2/{self.endpoint_id}/openai/v1"

    @property
    def timeout_seconds(self) -> float:
        return self.job_timeout_seconds

    @property
    def embedding_identity(self) -> str:
        return _embedding_identity(
            self.model_revision,
            {
                "provider": self.provider_id,
                "protocol": "runpod-queue+infinity-embeddings",
                "worker_protocol": self.worker_protocol,
                "endpoint_fingerprint": _runpod_endpoint_fingerprint(self.endpoint_id),
                "model": self.model,
                "weights_revision": self.model_revision,
                "deployment_revision": self.deployment_revision,
                "dimensions": self.dimensions,
            },
        )

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "protocol": "runpod-queue+infinity-embeddings",
            "worker_protocol": self.worker_protocol,
            "endpoint_origin": RUNPOD_API_ORIGIN,
            "endpoint_fingerprint": _runpod_endpoint_fingerprint(self.endpoint_id),
            "model": self.model,
            "model_revision": self.embedding_identity,
            "weights_revision": self.model_revision,
            "deployment_revision": self.deployment_revision,
            "dimensions": self.dimensions,
        }


@dataclass(frozen=True)
class InferenceConfig:
    profile_id: str
    structured_generation: OpenAIGenerationConfig | RunpodQueuedGenerationConfig | None
    embeddings: OpenAIEmbeddingConfig | RunpodQueuedEmbeddingConfig | None
    version: int = INFERENCE_CONFIG_VERSION


def configured_inference_path(explicit: Path | str | None = None) -> Path | None:
    """Resolve explicit CLI configuration before the non-secret environment path."""

    if explicit is not None:
        return Path(explicit).expanduser()
    value = os.environ.get(INFERENCE_CONFIG_ENV, "")
    if not value:
        return None
    if len(value) > 4096 or "\x00" in value:
        raise InferenceConfigError(f"{INFERENCE_CONFIG_ENV} is not a valid path")
    return Path(value).expanduser()


def _read_owner_only(path: Path, *, label: str, maximum: int) -> bytes:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise InferenceConfigError(
            f"{label} must be an owner-only regular file"
        ) from exc
    try:
        info = os.fstat(descriptor)
        current_uid = getattr(os, "geteuid", lambda: info.st_uid)()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != current_uid
        ):
            raise InferenceConfigError(f"{label} must be owner-only (mode 0600)")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not encoded or len(encoded) > maximum:
        raise InferenceConfigError(f"{label} is empty or too large")
    return encoded


def load_credential(path: Path) -> str:
    """Read a raw bearer token without exposing it in configuration or provenance."""

    encoded = _read_owner_only(
        path,
        label="inference credential file",
        maximum=MAX_CREDENTIAL_BYTES,
    )
    try:
        value = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise InferenceConfigError("inference credential must be UTF-8 text") from exc
    if not value or any(character.isspace() for character in value):
        raise InferenceConfigError(
            "inference credential must contain one non-whitespace token"
        )
    return value


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InferenceConfigError(f"{name} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unsupported " + ", ".join(extra))
        raise InferenceConfigError(
            f"{name} fields do not match version 1 ({'; '.join(details)})"
        )


def _text(value: Any, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InferenceConfigError(f"{name} must be bounded non-empty text")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise InferenceConfigError(f"{name} contains control characters")
    return value.strip()


def _identifier(value: Any, name: str) -> str:
    rendered = _text(value, name, 128)
    if not _IDENTIFIER.fullmatch(rendered):
        raise InferenceConfigError(f"{name} must be a portable identifier")
    return rendered


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InferenceConfigError(f"{name} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise InferenceConfigError(
            f"{name} must be between {minimum:g} and {maximum:g}"
        )
    return number


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise InferenceConfigError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _base_url(value: Any, name: str) -> str:
    raw = _text(value, name, 4096).rstrip("/")
    parsed = urlsplit(raw)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InferenceConfigError(
            f"{name} cannot contain credentials, a query, or a fragment"
        )
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and hostname in _LOOPBACK_HOSTS
    ):
        raise InferenceConfigError(
            f"{name} must use HTTPS (HTTP is allowed only for a loopback endpoint)"
        )
    if not hostname:
        raise InferenceConfigError(f"{name} must include a hostname")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _credential_path(value: Any, config_path: Path, name: str) -> Path:
    raw = _text(value, name, 4096)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    resolved = Path(os.path.abspath(path))
    # Validate now so a selected remote profile fails before any request is made.
    load_credential(resolved)
    return resolved


_COMMON_FIELDS = {
    "kind",
    "provider_id",
    "base_url",
    "model",
    "model_revision",
    "deployment_revision",
    "credential_file",
    "timeout_seconds",
    "max_response_bytes",
    "max_input_tokens",
}
_RUNPOD_COMMON_FIELDS = {
    "kind",
    "provider_id",
    "endpoint_id",
    "worker_protocol",
    "model",
    "model_revision",
    "deployment_revision",
    "credential_file",
    "request_timeout_seconds",
    "job_timeout_seconds",
    "poll_interval_seconds",
    "max_response_bytes",
    "max_input_tokens",
}


def _common(
    value: Mapping[str, Any],
    config_path: Path,
    name: str,
) -> dict[str, Any]:
    if value.get("kind") != "openai_compatible":
        raise InferenceConfigError(f"{name}.kind must be openai_compatible")
    model = _text(value.get("model"), f"{name}.model")
    revision = _text(value.get("model_revision"), f"{name}.model_revision")
    deployment = _text(
        value.get("deployment_revision"),
        f"{name}.deployment_revision",
    )
    if re.search(r"(?:^|[@/:])(?:main|master|latest)$", revision, re.IGNORECASE):
        raise InferenceConfigError(f"{name}.model_revision must be immutable")
    if re.search(r"(?:^|[@/:])(?:main|master|latest)$", deployment, re.IGNORECASE):
        raise InferenceConfigError(f"{name}.deployment_revision must be immutable")
    return {
        "provider_id": _identifier(value.get("provider_id"), f"{name}.provider_id"),
        "base_url": _base_url(value.get("base_url"), f"{name}.base_url"),
        "model": model,
        "model_revision": revision,
        "deployment_revision": deployment,
        "credential_file": _credential_path(
            value.get("credential_file"),
            config_path,
            f"{name}.credential_file",
        ),
        "timeout_seconds": _number(
            value.get("timeout_seconds"),
            f"{name}.timeout_seconds",
            1,
            600,
        ),
        "max_response_bytes": _integer(
            value.get("max_response_bytes"),
            f"{name}.max_response_bytes",
            1024,
            8 * 1024 * 1024,
        ),
        "max_input_tokens": _integer(
            value.get("max_input_tokens"),
            f"{name}.max_input_tokens",
            1024,
            262_144,
        ),
    }


def _runpod_common(
    value: Mapping[str, Any],
    config_path: Path,
    name: str,
    expected_worker_protocol: str,
) -> dict[str, Any]:
    if value.get("kind") != "runpod_queued":
        raise InferenceConfigError(f"{name}.kind must be runpod_queued")
    endpoint_id = _text(value.get("endpoint_id"), f"{name}.endpoint_id", 128)
    if not _RUNPOD_ENDPOINT_ID.fullmatch(endpoint_id):
        raise InferenceConfigError(f"{name}.endpoint_id is invalid")
    worker_protocol = _text(
        value.get("worker_protocol"), f"{name}.worker_protocol", 128
    )
    if worker_protocol != expected_worker_protocol:
        raise InferenceConfigError(
            f"{name}.worker_protocol must be {expected_worker_protocol}"
        )
    model = _text(value.get("model"), f"{name}.model", 255)
    if (
        not _MODEL_ID.fullmatch(model)
        or "//" in model
        or model.endswith("/")
        or any(part in {".", ".."} for part in model.split("/"))
    ):
        raise InferenceConfigError(f"{name}.model is invalid")
    model_revision = _text(value.get("model_revision"), f"{name}.model_revision", 384)
    prefix, separator, revision = model_revision.rpartition("@")
    if separator != "@" or prefix != model or not _HF_REVISION.fullmatch(revision):
        raise InferenceConfigError(
            f"{name}.model_revision must be <model>@<40-or-64-character HF commit>"
        )
    deployment_revision = _text(
        value.get("deployment_revision"), f"{name}.deployment_revision", 128
    )
    if not _IMAGE_DIGEST.fullmatch(deployment_revision):
        raise InferenceConfigError(
            f"{name}.deployment_revision must be an immutable sha256 image digest"
        )
    request_timeout = _number(
        value.get("request_timeout_seconds"),
        f"{name}.request_timeout_seconds",
        1,
        60,
    )
    job_timeout = _number(
        value.get("job_timeout_seconds"),
        f"{name}.job_timeout_seconds",
        10,
        3600,
    )
    if request_timeout > job_timeout:
        raise InferenceConfigError(
            f"{name}.request_timeout_seconds cannot exceed job_timeout_seconds"
        )
    return {
        "provider_id": _identifier(value.get("provider_id"), f"{name}.provider_id"),
        "endpoint_id": endpoint_id,
        "worker_protocol": worker_protocol,
        "model": model,
        "model_revision": model_revision,
        "deployment_revision": deployment_revision,
        "credential_file": _credential_path(
            value.get("credential_file"),
            config_path,
            f"{name}.credential_file",
        ),
        "request_timeout_seconds": request_timeout,
        "job_timeout_seconds": job_timeout,
        "poll_interval_seconds": _number(
            value.get("poll_interval_seconds"),
            f"{name}.poll_interval_seconds",
            0.05,
            10,
        ),
        "max_response_bytes": _integer(
            value.get("max_response_bytes"),
            f"{name}.max_response_bytes",
            1024,
            8 * 1024 * 1024,
        ),
        "max_input_tokens": _integer(
            value.get("max_input_tokens"),
            f"{name}.max_input_tokens",
            1024,
            262_144,
        ),
    }


def _generation(
    value: Any,
    config_path: Path,
) -> OpenAIGenerationConfig | RunpodQueuedGenerationConfig | None:
    if value is None:
        return None
    raw = _object(value, "structured_generation")
    if raw.get("kind") == "openrouter":
        expected = {"kind", "model", "credential_file", "timeout_seconds",
                    "max_response_bytes", "max_input_tokens", "default_max_output_tokens"}
        _exact_fields(raw, expected, "structured_generation")
        model = _text(raw.get("model"), "structured_generation.model", 255)
        if not _MODEL_ID.fullmatch(model) or "/" not in model:
            raise InferenceConfigError("OpenRouter requires an explicit model ID")
        common = _common({
            **raw, "kind": "openai_compatible", "provider_id": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "model_revision": model,
            "deployment_revision": "openrouter-routing-policy-v1",
        }, config_path, "structured_generation")
        return OpenRouterGenerationConfig(
            **common, json_schema_mode=True,
            default_max_output_tokens=_integer(raw.get("default_max_output_tokens"),
                "structured_generation.default_max_output_tokens", 1, 32768),
        )
    if raw.get("kind") == "runpod_queued":
        expected = _RUNPOD_COMMON_FIELDS | {
            "default_max_output_tokens",
            "json_schema_mode",
        }
        _exact_fields(raw, expected, "structured_generation")
        common = _runpod_common(
            raw,
            config_path,
            "structured_generation",
            RUNPOD_VLLM_PROXY_PROTOCOL,
        )
        schema_mode = raw.get("json_schema_mode")
        if not isinstance(schema_mode, bool):
            raise InferenceConfigError(
                "structured_generation.json_schema_mode must be boolean"
            )
        return RunpodQueuedGenerationConfig(
            **common,
            default_max_output_tokens=_integer(
                raw.get("default_max_output_tokens"),
                "structured_generation.default_max_output_tokens",
                1,
                32_768,
            ),
            json_schema_mode=schema_mode,
        )
    expected = _COMMON_FIELDS | {"default_max_output_tokens", "json_schema_mode"}
    _exact_fields(raw, expected, "structured_generation")
    common = _common(raw, config_path, "structured_generation")
    schema_mode = raw.get("json_schema_mode")
    if not isinstance(schema_mode, bool):
        raise InferenceConfigError(
            "structured_generation.json_schema_mode must be boolean"
        )
    return OpenAIGenerationConfig(
        **common,
        default_max_output_tokens=_integer(
            raw.get("default_max_output_tokens"),
            "structured_generation.default_max_output_tokens",
            1,
            32_768,
        ),
        json_schema_mode=schema_mode,
    )


def _embeddings(
    value: Any,
    config_path: Path,
) -> OpenAIEmbeddingConfig | RunpodQueuedEmbeddingConfig | None:
    if value is None:
        return None
    raw = _object(value, "embeddings")
    if raw.get("kind") == "runpod_queued":
        expected = _RUNPOD_COMMON_FIELDS | {"max_batch_size", "dimensions"}
        _exact_fields(raw, expected, "embeddings")
        common = _runpod_common(
            raw,
            config_path,
            "embeddings",
            RUNPOD_INFINITY_EMBEDDING_PROTOCOL,
        )
        return RunpodQueuedEmbeddingConfig(
            **common,
            max_batch_size=_integer(
                raw.get("max_batch_size"),
                "embeddings.max_batch_size",
                1,
                256,
            ),
            dimensions=_integer(
                raw.get("dimensions"),
                "embeddings.dimensions",
                1,
                32_768,
            ),
        )
    expected = _COMMON_FIELDS | {"max_batch_size", "dimensions"}
    _exact_fields(raw, expected, "embeddings")
    common = _common(raw, config_path, "embeddings")
    revision = str(common["model_revision"])
    if "@" not in revision or not all(revision.rsplit("@", 1)):
        raise InferenceConfigError(
            "embeddings.model_revision must be <model>@<immutable-revision>"
        )
    return OpenAIEmbeddingConfig(
        **common,
        max_batch_size=_integer(
            raw.get("max_batch_size"),
            "embeddings.max_batch_size",
            1,
            256,
        ),
        dimensions=_integer(raw.get("dimensions"), "embeddings.dimensions", 1, 32_768),
    )


def load_inference_config(path: Path | str) -> InferenceConfig:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    # ``abspath`` normalizes navigation without dereferencing the final component;
    # O_NOFOLLOW in _read_owner_only must still be able to reject a symlink.
    target = Path(os.path.abspath(target))
    encoded = _read_owner_only(
        target,
        label="inference config",
        maximum=MAX_CONFIG_BYTES,
    )
    try:
        raw = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InferenceConfigError("inference config must be valid UTF-8 JSON") from exc
    config = _object(raw, "inference config")
    _exact_fields(
        config,
        {"version", "profile_id", "structured_generation", "embeddings"},
        "inference config",
    )
    if config.get("version") != INFERENCE_CONFIG_VERSION:
        raise InferenceConfigError("inference config version must be 1")
    result = InferenceConfig(
        profile_id=_identifier(config.get("profile_id"), "profile_id"),
        structured_generation=_generation(config.get("structured_generation"), target),
        embeddings=_embeddings(config.get("embeddings"), target),
    )
    if result.structured_generation is None and result.embeddings is None:
        raise InferenceConfigError(
            "inference config must enable at least one capability"
        )
    return result
