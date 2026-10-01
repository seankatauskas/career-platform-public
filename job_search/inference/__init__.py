"""Portable inference configuration and OpenAI-compatible providers."""

from .config import (
    INFERENCE_CONFIG_ENV,
    InferenceConfig,
    OpenAIEmbeddingConfig,
    OpenAIGenerationConfig,
    RunpodQueuedEmbeddingConfig,
    RunpodQueuedGenerationConfig,
    configured_inference_path,
    load_inference_config,
)
from .contracts import (
    EmbeddingProvider,
    GenerationResult,
    InferenceConfigError,
    InferenceTransportError,
    StructuredGenerationProvider,
)
from .providers import (
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleStructuredGenerator,
    RunpodQueuedEmbeddingProvider,
    RunpodQueuedStructuredGenerator,
)


def build_structured_provider(config: InferenceConfig) -> StructuredGenerationProvider:
    if config.structured_generation is None:
        raise InferenceConfigError(
            f"inference profile {config.profile_id!r} has no structured_generation capability"
        )
    if isinstance(config.structured_generation, RunpodQueuedGenerationConfig):
        return RunpodQueuedStructuredGenerator(config.structured_generation)
    return OpenAICompatibleStructuredGenerator(config.structured_generation)


def build_embedding_provider(config: InferenceConfig) -> EmbeddingProvider:
    if config.embeddings is None:
        raise InferenceConfigError(
            f"inference profile {config.profile_id!r} has no embeddings capability"
        )
    if isinstance(config.embeddings, RunpodQueuedEmbeddingConfig):
        return RunpodQueuedEmbeddingProvider(config.embeddings)
    return OpenAICompatibleEmbeddingProvider(config.embeddings)


__all__ = [
    "INFERENCE_CONFIG_ENV",
    "EmbeddingProvider",
    "GenerationResult",
    "InferenceConfig",
    "InferenceConfigError",
    "InferenceTransportError",
    "OpenAICompatibleEmbeddingProvider",
    "OpenAICompatibleStructuredGenerator",
    "OpenAIEmbeddingConfig",
    "OpenAIGenerationConfig",
    "RunpodQueuedEmbeddingConfig",
    "RunpodQueuedEmbeddingProvider",
    "RunpodQueuedGenerationConfig",
    "RunpodQueuedStructuredGenerator",
    "StructuredGenerationProvider",
    "build_embedding_provider",
    "build_structured_provider",
    "configured_inference_path",
    "load_inference_config",
]
