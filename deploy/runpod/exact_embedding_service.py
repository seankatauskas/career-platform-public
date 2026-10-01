"""Exact-revision replacement for worker-infinity's embedding service.

This file is copied over ``/embedding_service.py`` in a digest-pinned derivative
worker image.  The upstream Runpod handler and response helpers remain in use, but
model loading is narrowed to one pre-cached Hugging Face snapshot.  Network fallback,
mutable revisions, remote model code, and unverified served names are all rejected.

The upstream worker and Infinity are MIT licensed.  This replacement follows their
public service interface while adding the model-identity checks needed by this repo.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

WORKER_PROTOCOL = "job_search_infinity_exact_v1"
RUNPOD_HF_HUB_CACHE = Path("/runpod-volume/huggingface-cache/hub")
_MODEL_ID = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def resolve_exact_snapshot(
    cache_root: Path, model_id: str, revision: str
) -> Path:
    """Resolve one immutable Runpod model-cache snapshot without a network lookup."""

    if not _MODEL_ID.fullmatch(model_id):
        raise ValueError("MODEL_NAMES must contain one Hugging Face org/repository id")
    if not _REVISION.fullmatch(revision):
        raise ValueError("MODEL_REVISION must be an immutable Hugging Face commit")
    root = Path(cache_root).resolve(strict=True)
    repository = "models--" + model_id.replace("/", "--")
    # Runpod's model cache has used both canonical and lower-cased repository paths.
    relative_names = (repository, repository.lower())
    matches: list[Path] = []
    for relative_name in dict.fromkeys(relative_names):
        candidate = root / relative_name / "snapshots" / revision
        if not candidate.is_dir():
            continue
        resolved = candidate.resolve(strict=True)
        if resolved != root and root not in resolved.parents:
            raise ValueError("model snapshot escapes the Runpod cache root")
        if not any(os.path.samefile(resolved, previous) for previous in matches):
            matches.append(resolved)
    if len(matches) != 1:
        raise ValueError("exact model snapshot is missing or ambiguous in Runpod cache")
    return matches[0]


class EmbeddingService:
    """Drop-in upstream service that loads and attests one exact local snapshot."""

    def __init__(self) -> None:
        from config import EmbeddingServiceConfig
        from infinity_emb.engine import AsyncEngineArray, EngineArgs

        config = EmbeddingServiceConfig()
        if len(config.model_names) != 1:
            raise ValueError("exact embedding worker supports exactly one model")
        model_id = config.model_names[0]
        revision = os.environ.get("MODEL_REVISION", "")
        protocol = os.environ.get("JOB_SEARCH_EMBEDDING_PROTOCOL", "")
        hub_cache = Path(os.environ.get("HF_HUB_CACHE", ""))
        if protocol != WORKER_PROTOCOL:
            raise ValueError("embedding worker protocol identity is invalid")
        if hub_cache != RUNPOD_HF_HUB_CACHE:
            raise ValueError("HF_HUB_CACHE must use the Runpod model-cache location")
        if os.environ.get("HF_HUB_OFFLINE") != "1":
            raise ValueError("HF_HUB_OFFLINE=1 is required")
        if os.environ.get("TRANSFORMERS_OFFLINE") != "1":
            raise ValueError("TRANSFORMERS_OFFLINE=1 is required")
        snapshot = resolve_exact_snapshot(hub_cache, model_id, revision)
        engine = EngineArgs(
            model_name_or_path=str(snapshot),
            served_model_name=model_id,
            revision=None,
            trust_remote_code=False,
            batch_size=config.batch_sizes[0],
            engine=config.backend,
            dtype=config.dtypes[0],
            model_warmup=False,
            lengths_via_tokenize=True,
        )
        self.config = config
        self.model_id = model_id
        self.model_revision = revision
        self.snapshot = snapshot
        self.engine_array = AsyncEngineArray.from_args([engine])
        self.is_running = False
        self.sepamore = asyncio.Semaphore(1)

    async def start(self) -> None:
        async with self.sepamore:
            if not self.is_running:
                await self.engine_array.astart()
                self.is_running = True

    async def stop(self) -> None:
        async with self.sepamore:
            if self.is_running:
                await self.engine_array.astop()
                self.is_running = False

    def list_models(self) -> list[str]:
        return [self.model_id]

    async def route_openai_models(self) -> dict[str, Any]:
        from utils import ModelInfo, OpenAIModelInfo

        return OpenAIModelInfo(
            data=[ModelInfo(id=self.model_id, stats={})]
        ).model_dump()

    def _attest(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            value = dict(value)
        result = dict(value)
        result["job_search_model_revision"] = self.model_revision
        result["job_search_worker_protocol"] = WORKER_PROTOCOL
        return result

    async def route_openai_get_embeddings(
        self,
        embedding_input: str | list[str],
        model_name: str,
        return_as_list: bool = False,
    ) -> Any:
        from utils import list_embeddings_to_response

        if model_name != self.model_id:
            raise ValueError("requested model does not match the pinned model")
        if not self.is_running:
            await self.start()
        inputs = embedding_input if isinstance(embedding_input, list) else [embedding_input]
        embeddings, usage = await self.engine_array[self.model_id].embed(inputs)
        result = self._attest(
            list_embeddings_to_response(
                embeddings, model=self.model_id, usage=usage
            )
        )
        return [result] if return_as_list else result

    async def infinity_rerank(self, **_kwargs: Any) -> Any:
        raise ValueError("reranking is disabled on this embedding-only worker")
