from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .config import Settings, get_settings


class Mem0ConfigError(RuntimeError):
    pass


def _qdrant_config_from_url(url: str, *, collection_name: str, embedding_dims: int, api_key: str | None) -> dict[str, Any]:
    """Build a Mem0-compatible Qdrant vector_store config.

    Mem0 accepts either host+port or url+api_key. For local Windows/Docker use we
    prefer host+port to avoid requiring a fake api_key for http://localhost.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        raise Mem0ConfigError(f"MLOMEGA_MEM0_QDRANT_URL invalide: {url!r}")

    cfg: dict[str, Any] = {
        "collection_name": collection_name,
        "embedding_model_dims": embedding_dims,
    }
    if api_key:
        cfg.update({"url": url.rstrip("/"), "api_key": api_key})
    else:
        cfg.update({"host": parsed.hostname, "port": parsed.port or (443 if parsed.scheme == "https" else 6333)})
    return cfg


def build_mem0_config(settings: Settings | None = None) -> dict[str, Any]:
    """Return the one canonical Mem0 config used by sync, doctor and tests.

    Everything is wired to local providers by default:
    - LLM: Ollama using MLOMEGA_MEM0_LLM_MODEL / MLOMEGA_OLLAMA_MODEL
    - Embedder: Ollama using MLOMEGA_MEM0_EMBEDDER_MODEL
    - Vector store: local Qdrant, isolated collection mlomega_mem0

    This prevents Mem0 from silently falling back to OpenAI or another hosted
    provider when running the local memory foundation.
    """
    s = settings or get_settings()
    if not s.mem0_enabled:
        raise Mem0ConfigError("Mem0 est désactivé: MLOMEGA_MEM0_ENABLED=false")
    if s.mem0_vector_store.lower() != "qdrant":
        raise Mem0ConfigError("Cette build élite supporte Mem0 local uniquement via Qdrant")

    config: dict[str, Any] = {
        "version": "v1.1",
        "llm": {
            "provider": s.mem0_llm_provider,
            "config": {
                "model": s.mem0_llm_model,
                "temperature": s.mem0_llm_temperature,
                "max_tokens": s.mem0_llm_max_tokens,
            },
        },
        "embedder": {
            "provider": s.mem0_embedder_provider,
            "config": {
                "model": s.mem0_embedder_model,
                "embedding_dims": s.mem0_embedding_dims,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": _qdrant_config_from_url(
                s.mem0_qdrant_url,
                collection_name=s.mem0_qdrant_collection,
                embedding_dims=s.mem0_embedding_dims,
                api_key=s.mem0_qdrant_api_key,
            ),
        },
        # Supported by recent Mem0 builds; harmless if ignored by older builds.
        "history_db_path": str(s.mem0_history_db_path),
    }

    if s.mem0_llm_provider.lower() == "ollama":
        config["llm"]["config"]["ollama_base_url"] = s.ollama_base_url
    if s.mem0_embedder_provider.lower() == "ollama":
        config["embedder"]["config"]["ollama_base_url"] = s.ollama_base_url
    return config


def create_mem0_memory() -> Any:
    """Instantiate Mem0 using the canonical local config."""
    try:
        from mem0 import Memory
    except Exception as exc:  # pragma: no cover - requires elite deps
        raise Mem0ConfigError("mem0ai absent. Installe le profil graph/all du projet.") from exc

    config = build_mem0_config()
    if not hasattr(Memory, "from_config"):
        raise Mem0ConfigError("Version mem0ai trop ancienne: Memory.from_config introuvable")
    try:
        return Memory.from_config(config)
    except TypeError:
        # Some mem0ai releases used a keyword-only config_dict parameter.
        return Memory.from_config(config_dict=config)
