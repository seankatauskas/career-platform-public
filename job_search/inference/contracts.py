"""Small, provider-neutral contracts for remote inference.

The application deliberately depends on these contracts rather than on a vendor SDK.
That keeps deterministic processing portable while model execution can move between a
local machine, Runpod, or another explicitly configured OpenAI-compatible service.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class InferenceConfigError(ValueError):
    """An inference profile or its credential does not satisfy the security contract."""


class InferenceTransportError(RuntimeError):
    """A bounded remote inference request failed.

    ``retryable`` is advisory for the caller's durable queue.  Providers never retry or
    switch models internally, so an ambiguous request cannot quietly incur duplicate
    work or produce output from a different model.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status_code = status_code


class InferenceResponseRejected(InferenceTransportError):
    """A received response failed validation; submission is not ambiguous."""


@dataclass(frozen=True)
class GenerationResult:
    text: str
    usage: Mapping[str, Any]
    provenance: Mapping[str, Any]


class StructuredGenerationProvider(Protocol):
    model_revision: str
    generation_identity: str
    max_input_tokens: int

    @property
    def provenance(self) -> Mapping[str, Any]: ...

    def count_tokens_upper_bound(self, text: str) -> int: ...

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
    ) -> GenerationResult: ...


class EmbeddingProvider(Protocol):
    model_revision: str

    @property
    def provenance(self) -> Mapping[str, Any]: ...

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...
