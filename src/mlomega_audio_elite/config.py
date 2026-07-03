from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_files() -> None:
    """Load .env from the current project tree when python-dotenv is available.

    The Windows scripts also export variables explicitly, but auto-loading .env
    makes direct `mlomega-audio ...` commands behave predictably from the
    project root. Existing process environment variables still win.
    """
    try:
        from dotenv import find_dotenv, load_dotenv

        env_path = find_dotenv(filename=".env", usecwd=True)
        if env_path:
            load_dotenv(env_path, override=False)
    except Exception:
        return


_load_env_files()


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


def _csv_floats(name: str, default: str) -> tuple[float, ...]:
    raw = os.environ.get(name, default)
    values: list[float] = []
    for item in str(raw).split(","):
        try:
            value = float(item.strip())
        except (TypeError, ValueError):
            continue
        if value >= 0:
            values.append(value)
    return tuple(values) or tuple(float(x) for x in default.split(","))


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    db_path: Path
    raw_dir: Path
    vector_backend: str
    qdrant_url: str
    qdrant_collection: str
    qdrant_api_key: str | None
    v17_qdrant_collection: str
    v17_embedding_revision: str
    v17_dense_candidate_limit: int
    v17_rerank_candidate_limit: int
    v17_calibration_min_samples: int
    v17_calibration_min_validation_precision: float
    lancedb_uri: str
    embedding_backend: str
    embedding_model: str
    reranker_backend: str
    reranker_model: str
    graph_backend: str
    graphiti_uri: str
    graphiti_user: str
    graphiti_password: str
    ollama_base_url: str
    ollama_model: str
    enable_ollama: bool
    enable_llm_deep: bool
    enable_whisperx: bool
    enable_pyannote: bool
    enable_speechbrain: bool
    strict_elite: bool
    hf_token: str | None
    whisperx_model: str
    whisperx_device: str
    whisperx_compute_type: str
    whisperx_batch_size: int
    voice_threshold: float
    mem0_enabled: bool
    mem0_llm_provider: str
    mem0_llm_model: str
    mem0_llm_temperature: float
    mem0_llm_max_tokens: int
    mem0_embedder_provider: str
    mem0_embedder_model: str
    mem0_embedding_dims: int
    mem0_vector_store: str
    mem0_qdrant_url: str
    mem0_qdrant_collection: str
    mem0_qdrant_api_key: str | None
    mem0_history_db_path: Path
    # V18.7 core-production delivery profile.  These knobs are deliberately
    # first-class Settings so the daily close path cannot silently fall back to
    # short command-line defaults.
    deployment_profile: str
    require_self_voice: bool
    voice_learning_strict: bool
    ollama_connect_timeout_s: float
    ollama_cold_start_timeout_s: float
    poststop_llm_timeout_s: float
    poststop_vlm_timeout_s: float
    poststop_retry_max: int
    poststop_retry_backoff_seconds: tuple[float, ...]
    deep_audio_bundle_max_seconds: float
    deep_audio_ffmpeg_timeout_s: float
    deep_audio_retry_max: int
    stage_stale_after_s: int
    cleanup_requires_zero_pending: bool
    ollama_keep_alive_live: str
    ollama_keep_alive_poststop: str
    phone_bridge_url: str | None
    phone_bridge_required: bool


def get_settings() -> Settings:
    root = Path(os.environ.get("MLOMEGA_HOME", Path.cwd() / ".mlomega_audio_elite")).expanduser().resolve()
    db_path = Path(os.environ.get("MLOMEGA_DB", root / "memory.db")).expanduser().resolve()
    raw_dir = Path(os.environ.get("MLOMEGA_RAW", root / "raw")).expanduser().resolve()
    return Settings(
        root_dir=root,
        db_path=db_path,
        raw_dir=raw_dir,
        vector_backend=os.environ.get("MLOMEGA_VECTOR_BACKEND", "qdrant"),
        qdrant_url=os.environ.get("MLOMEGA_QDRANT_URL", "http://localhost:6333"),
        qdrant_collection=os.environ.get("MLOMEGA_QDRANT_COLLECTION", "mlomega_audio_memory"),
        qdrant_api_key=os.environ.get("MLOMEGA_QDRANT_API_KEY") or os.environ.get("QDRANT_API_KEY"),
        # V17 uses a versioned *collection namespace* in the same Qdrant
        # instance.  It must not mix case vectors with generic retrieval chunks
        # whose model/dimensions/provenance may differ.
        v17_qdrant_collection=os.environ.get(
            "MLOMEGA_V17_QDRANT_COLLECTION",
            f"{os.environ.get('MLOMEGA_QDRANT_COLLECTION', 'mlomega_audio_memory')}_v17_cases",
        ),
        v17_embedding_revision=os.environ.get("MLOMEGA_V17_EMBEDDING_REVISION", "v18-rc5-predictive-1"),
        v17_dense_candidate_limit=max(1, int(os.environ.get("MLOMEGA_V17_DENSE_CANDIDATE_LIMIT", "80"))),
        v17_rerank_candidate_limit=max(1, int(os.environ.get("MLOMEGA_V17_RERANK_CANDIDATE_LIMIT", "30"))),
        v17_calibration_min_samples=max(6, int(os.environ.get("MLOMEGA_V17_CALIBRATION_MIN_SAMPLES", "30"))),
        v17_calibration_min_validation_precision=float(os.environ.get("MLOMEGA_V17_CALIBRATION_MIN_VALIDATION_PRECISION", "0.60")),
        lancedb_uri=os.environ.get("MLOMEGA_LANCEDB_URI", str(root / "lancedb")),
        embedding_backend=os.environ.get("MLOMEGA_EMBEDDING_BACKEND", "sentence_transformers"),
        embedding_model=os.environ.get("MLOMEGA_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B"),
        reranker_backend=os.environ.get("MLOMEGA_RERANKER_BACKEND", "sentence_transformers"),
        reranker_model=os.environ.get("MLOMEGA_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
        graph_backend=os.environ.get("MLOMEGA_GRAPH_BACKEND", "disabled"),
        graphiti_uri=os.environ.get("MLOMEGA_GRAPHITI_URI", os.environ.get("NEO4J_URI", "bolt://localhost:7687")),
        graphiti_user=os.environ.get("MLOMEGA_GRAPHITI_USER", os.environ.get("NEO4J_USER", "neo4j")),
        graphiti_password=os.environ.get("MLOMEGA_GRAPHITI_PASSWORD", os.environ.get("NEO4J_PASSWORD", "mlomega-password")),
        ollama_base_url=os.environ.get("MLOMEGA_OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.environ.get("MLOMEGA_OLLAMA_MODEL", "qwen3.5:9b"),
        enable_ollama=_bool("MLOMEGA_ENABLE_OLLAMA", "true"),
        enable_llm_deep=_bool("MLOMEGA_ENABLE_LLM_DEEP", "true"),
        enable_whisperx=_bool("MLOMEGA_ENABLE_WHISPERX", "true"),
        enable_pyannote=_bool("MLOMEGA_ENABLE_PYANNOTE", "true"),
        enable_speechbrain=_bool("MLOMEGA_ENABLE_SPEECHBRAIN", "true"),
        strict_elite=_bool("MLOMEGA_STRICT_ELITE", "true"),
        hf_token=os.environ.get("MLOMEGA_HF_TOKEN") or os.environ.get("HF_TOKEN"),
        whisperx_model=os.environ.get("MLOMEGA_WHISPERX_MODEL", "large-v3"),
        whisperx_device=os.environ.get("MLOMEGA_WHISPERX_DEVICE", "cuda"),
        whisperx_compute_type=os.environ.get("MLOMEGA_WHISPERX_COMPUTE_TYPE", "float16"),
        whisperx_batch_size=int(os.environ.get("MLOMEGA_WHISPERX_BATCH_SIZE", "4")),
        voice_threshold=float(os.environ.get("MLOMEGA_VOICE_THRESHOLD", "0.72")),
        mem0_enabled=_bool("MLOMEGA_MEM0_ENABLED", "false"),
        mem0_llm_provider=os.environ.get("MLOMEGA_MEM0_LLM_PROVIDER", "ollama"),
        mem0_llm_model=os.environ.get("MLOMEGA_MEM0_LLM_MODEL", os.environ.get("MLOMEGA_OLLAMA_MODEL", "qwen3.5:9b")),
        mem0_llm_temperature=float(os.environ.get("MLOMEGA_MEM0_LLM_TEMPERATURE", "0.1")),
        mem0_llm_max_tokens=int(os.environ.get("MLOMEGA_MEM0_LLM_MAX_TOKENS", "2000")),
        mem0_embedder_provider=os.environ.get("MLOMEGA_MEM0_EMBEDDER_PROVIDER", "ollama"),
        mem0_embedder_model=os.environ.get("MLOMEGA_MEM0_EMBEDDER_MODEL", "nomic-embed-text"),
        mem0_embedding_dims=int(os.environ.get("MLOMEGA_MEM0_EMBEDDING_DIMS", "768")),
        mem0_vector_store=os.environ.get("MLOMEGA_MEM0_VECTOR_STORE", "qdrant"),
        mem0_qdrant_url=os.environ.get("MLOMEGA_MEM0_QDRANT_URL", os.environ.get("MLOMEGA_QDRANT_URL", "http://localhost:6333")),
        mem0_qdrant_collection=os.environ.get("MLOMEGA_MEM0_QDRANT_COLLECTION", "mlomega_mem0"),
        mem0_qdrant_api_key=os.environ.get("MLOMEGA_MEM0_QDRANT_API_KEY") or os.environ.get("QDRANT_API_KEY"),
        mem0_history_db_path=Path(os.environ.get("MLOMEGA_MEM0_HISTORY_DB", root / "mem0_history.db")).expanduser().resolve(),
        deployment_profile=os.environ.get("MLOMEGA_DEPLOYMENT_PROFILE", "CORE_BRAINLIVE_V18_8_PHONE"),
        require_self_voice=_bool("MLOMEGA_REQUIRE_SELF_VOICE", "false"),
        voice_learning_strict=_bool("MLOMEGA_VOICE_LEARNING_STRICT", "true"),
        ollama_connect_timeout_s=_float("MLOMEGA_OLLAMA_CONNECT_TIMEOUT_S", 30.0, minimum=1.0),
        ollama_cold_start_timeout_s=_float("MLOMEGA_OLLAMA_COLD_START_TIMEOUT_S", 600.0, minimum=1.0),
        poststop_llm_timeout_s=_float("MLOMEGA_POSTSTOP_LLM_TIMEOUT_S", 900.0, minimum=1.0),
        poststop_vlm_timeout_s=_float("MLOMEGA_POSTSTOP_VLM_TIMEOUT_S", 300.0, minimum=1.0),
        poststop_retry_max=_int("MLOMEGA_POSTSTOP_RETRY_MAX", 2, minimum=0),
        poststop_retry_backoff_seconds=_csv_floats("MLOMEGA_POSTSTOP_RETRY_BACKOFF_S", "15,60"),
        deep_audio_bundle_max_seconds=_float("MLOMEGA_DEEP_AUDIO_BUNDLE_MAX_SECONDS", 1800.0, minimum=1.0),
        deep_audio_ffmpeg_timeout_s=_float("MLOMEGA_DEEP_AUDIO_FFMPEG_TIMEOUT_S", 300.0, minimum=1.0),
        deep_audio_retry_max=_int("MLOMEGA_DEEP_AUDIO_RETRY_MAX", 1, minimum=0),
        stage_stale_after_s=_int("MLOMEGA_STAGE_STALE_AFTER_S", 1800, minimum=1),
        cleanup_requires_zero_pending=_bool("MLOMEGA_CLEANUP_REQUIRES_ZERO_PENDING", "true"),
        ollama_keep_alive_live=os.environ.get("MLOMEGA_OLLAMA_KEEP_ALIVE_LIVE", "10m"),
        ollama_keep_alive_poststop=os.environ.get("MLOMEGA_OLLAMA_KEEP_ALIVE_POSTSTOP", "15m"),
        phone_bridge_url=os.environ.get("MLOMEGA_PHONE_BRIDGE_URL", "http://127.0.0.1:8766") or None,
        phone_bridge_required=_bool("MLOMEGA_PHONE_BRIDGE_REQUIRED", "true"),
    )


def is_core_brainlive_phone_profile(profile: str | None) -> bool:
    """Return whether a graph-free production core profile is selected.

    V18.7 remains accepted for an in-place upgrade, while V18.8 is the current
    canonical release profile.  Both intentionally exclude Graphiti/Neo4j/Mem0.
    """
    return str(profile or "").strip().upper() in {
        "CORE_BRAINLIVE_V18_7_PHONE",
        "CORE_BRAINLIVE_V18_8_PHONE",
    }


def validate_core_profile(settings: Settings | None = None) -> list[str]:
    """Return deterministic configuration errors for supported graph-free core profiles."""
    cfg = settings or get_settings()
    errors: list[str] = []
    if not is_core_brainlive_phone_profile(cfg.deployment_profile):
        errors.append("MLOMEGA_DEPLOYMENT_PROFILE must be CORE_BRAINLIVE_V18_8_PHONE (V18.7 accepted only for upgrade compatibility)")
    if cfg.graph_backend.lower() not in {"disabled", "none", "off"}:
        errors.append("MLOMEGA_GRAPH_BACKEND must be disabled in the core V18.8 profile")
    if cfg.mem0_enabled:
        errors.append("MLOMEGA_MEM0_ENABLED must be false in the core V18.8 profile")
    if not cfg.enable_whisperx or not cfg.enable_pyannote or not cfg.enable_speechbrain:
        errors.append("WhisperX, Pyannote and SpeechBrain must all be enabled")
    if not cfg.hf_token or str(cfg.hf_token).strip().upper().startswith(("YOUR_", "CHANGE_ME", "TODO")):
        errors.append("MLOMEGA_HF_TOKEN is missing or a placeholder")
    if cfg.whisperx_device.lower() != "cuda" or cfg.whisperx_compute_type.lower() != "float16":
        errors.append("production profile requires WhisperX CUDA float16")
    if not cfg.enable_ollama:
        errors.append("MLOMEGA_ENABLE_OLLAMA must be true")
    if not cfg.ollama_model:
        errors.append("MLOMEGA_OLLAMA_MODEL is required")
    if cfg.cleanup_requires_zero_pending is not True:
        errors.append("MLOMEGA_CLEANUP_REQUIRES_ZERO_PENDING must be true")
    if cfg.phone_bridge_required and not cfg.phone_bridge_url:
        errors.append("MLOMEGA_PHONE_BRIDGE_URL is required when the phone bridge is required")
    if cfg.require_self_voice:
        errors.append("MLOMEGA_REQUIRE_SELF_VOICE must remain false for zero-touch bootstrap; enroll the owner voice later through voice-pending")
    return errors
