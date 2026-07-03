from __future__ import annotations

"""V18.7 local-runtime hardening.

This module keeps operational concerns out of the memory logic:

* classify failures as retryable vs evidence/configuration blocks;
* apply bounded retries with a deterministic, inspectable backoff policy;
* keep heavyweight offline audio models resident for one post-stop phase only;
* release CUDA allocations between deep-audio, VLM and Brain2 phases;
* retain a small process-local cache for VAD, embeddings, rerankers and Qdrant.

Nothing here converts an error into a successful result.  A caller still has to
persist the returned/raised state before allowing a cleanup gate.
"""

import contextlib
import contextvars
import gc
import json
import os
import random
import socket
import sqlite3
import threading
import uuid
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from .config import get_settings
from .utils import now_iso

T = TypeVar("T")

_current_phase: contextvars.ContextVar[str] = contextvars.ContextVar("mlomega_runtime_phase", default="live")
_CACHE_LOCK = threading.RLock()
_PROCESS_CACHE: dict[tuple[str, ...], Any] = {}


class RuntimePolicyError(RuntimeError):
    """Base operational error carrying a safe classification."""

    def __init__(self, message: str, *, code: str, retryable: bool, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.cause = cause


@dataclass(frozen=True)
class FailureClassification:
    code: str
    retryable: bool
    message: str


@dataclass(frozen=True)
class ExecutionLease:
    """Ownership of one locally executing resumable pipeline run.

    The lease is stored in SQLite rather than only in a lock file.  A Windows
    shutdown may leave a file behind; a persisted PID/host/token lets the next
    process distinguish a living worker from a dead owner and safely reclaim
    exactly the same logical run.  ``release`` deletes only the caller's token,
    so it cannot remove a newer owner's lease.
    """

    run_id: str
    purpose: str
    owner_token: str
    acquired: bool
    reclaimed: bool = False
    owner_pid: int | None = None
    owner_host: str | None = None

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            from .db import connect, write_transaction
            with connect() as con, write_transaction(con):
                con.execute(
                    "DELETE FROM v18_execution_leases_v186 WHERE run_id=? AND owner_token=?",
                    (self.run_id, self.owner_token),
                )
        except Exception:
            # A lease that survives an abrupt shutdown is intentionally safe:
            # the next local process can reclaim it only after proving its PID
            # is no longer alive.  Never mask pipeline results with cleanup I/O.
            pass


def _execution_lease_schema() -> None:
    from .db import connect, init_db, write_transaction
    init_db()
    with connect() as con, write_transaction(con):
        con.execute(
            """CREATE TABLE IF NOT EXISTS v18_execution_leases_v186 (
                 run_id TEXT PRIMARY KEY,
                 purpose TEXT NOT NULL,
                 owner_pid INTEGER NOT NULL,
                 owner_host TEXT NOT NULL,
                 owner_token TEXT NOT NULL,
                 acquired_at TEXT NOT NULL,
                 heartbeat_at TEXT NOT NULL
               )"""
        )


def is_local_pid_alive(pid: int | None) -> bool:
    """Return whether a local PID is still alive on Windows and POSIX.

    `os.kill(pid, 0)` is a useful POSIX liveness probe but its signal semantics
    are not a safe ownership primitive on Windows.  Windows uses
    OpenProcess/GetExitCodeProcess instead, so an active post-stop lease cannot
    be reclaimed merely because a Unix-only probe behaved differently.
    """
    if not pid or int(pid) <= 0:
        return False
    value = int(pid)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, value)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return int(exit_code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Failing closed preserves the live owner rather than risking two
            # writers. The stale heartbeat path remains available later.
            return True
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Permission denied means another local process exists.  Treat it as
        # live; taking over could duplicate a real post-stop run.
        return True
    except OSError:
        return False
    return True


def acquire_execution_lease(*, run_id: str, purpose: str) -> ExecutionLease:
    """Claim exclusive local execution of ``run_id``.

    A competing live process receives ``acquired=False``.  After a power loss,
    the old PID is dead and the first resume atomically replaces that row; this
    is what makes immediate resume safe without an arbitrary stale timeout.
    The deployment is intentionally single-host; a foreign-host row is never
    stolen automatically.
    """
    if not run_id:
        raise RuntimePolicyError("execution lease requires run_id", code="blocked_contract", retryable=False)
    _execution_lease_schema()
    from .db import connect, write_transaction
    host = socket.gethostname()
    pid = os.getpid()
    token = uuid.uuid4().hex
    now = now_iso()
    with connect() as con, write_transaction(con):
        row = con.execute(
            "SELECT purpose,owner_pid,owner_host,owner_token FROM v18_execution_leases_v186 WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO v18_execution_leases_v186(run_id,purpose,owner_pid,owner_host,owner_token,acquired_at,heartbeat_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, purpose, pid, host, token, now, now),
            )
            return ExecutionLease(run_id, purpose, token, True, False, pid, host)
        owner = dict(row)
        owner_pid = int(owner.get("owner_pid") or 0)
        owner_host = str(owner.get("owner_host") or "")
        if owner_host == host and is_local_pid_alive(owner_pid):
            return ExecutionLease(run_id, purpose, token, False, False, owner_pid, owner_host)
        if owner_host and owner_host != host:
            return ExecutionLease(run_id, purpose, token, False, False, owner_pid, owner_host)
        con.execute(
            "UPDATE v18_execution_leases_v186 SET purpose=?,owner_pid=?,owner_host=?,owner_token=?,acquired_at=?,heartbeat_at=? WHERE run_id=?",
            (purpose, pid, host, token, now, now, run_id),
        )
        return ExecutionLease(run_id, purpose, token, True, True, pid, host)


def heartbeat_execution_lease(lease: ExecutionLease) -> None:
    """Refresh a held lease for observability; PID liveness remains primary."""
    if not lease.acquired:
        return
    try:
        from .db import connect, write_transaction
        with connect() as con, write_transaction(con):
            con.execute(
                "UPDATE v18_execution_leases_v186 SET heartbeat_at=? WHERE run_id=? AND owner_token=?",
                (now_iso(), lease.run_id, lease.owner_token),
            )
    except Exception:
        pass


def runtime_phase() -> str:
    return _current_phase.get()


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    token = _current_phase.set(str(name))
    try:
        yield
    finally:
        _current_phase.reset(token)


def _lower_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".lower()


def classify_failure(exc: BaseException) -> FailureClassification:
    """Map only well-understood local failures to a retry policy.

    Unknown programming/data errors are blocked, never retried indefinitely.
    """
    if isinstance(exc, RuntimePolicyError):
        return FailureClassification(exc.code, bool(exc.retryable), str(exc))
    text = _lower_error(exc)
    if isinstance(exc, sqlite3.OperationalError) and any(x in text for x in ("locked", "busy")):
        return FailureClassification("sqlite_busy", True, "SQLite temporairement verrouillée")
    if isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError)):
        return FailureClassification("transport_timeout", True, "Service local indisponible ou délai dépassé")
    if isinstance(exc, urllib.error.HTTPError):
        code = int(getattr(exc, "code", 0) or 0)
        if code in {408, 429, 500, 502, 503, 504}:
            return FailureClassification(f"http_{code}", True, f"Erreur HTTP transitoire {code}")
        return FailureClassification(f"http_{code or 'error'}", False, f"Erreur HTTP non récupérable {code}")
    if any(x in text for x in ("timed out", "timeout", "connection refused", "connection reset", "temporarily unavailable", "service unavailable", "http error 503", "http error 429", "retryable_error", "deep_audio_retryable", "vlm_retryable", "retryable")):
        return FailureClassification("transport_timeout", True, "Service local indisponible ou délai dépassé")
    if any(x in text for x in ("out of memory", "cuda oom", "cuda error: out of memory", "cublas_status_alloc_failed")):
        return FailureClassification("gpu_oom", True, "Mémoire GPU insuffisante temporairement")
    if any(x in text for x in ("hf_token", "huggingface", "gated", "401", "403", "unauthorized", "forbidden")):
        return FailureClassification("blocked_hf_access", False, "Accès Hugging Face/Pyannote non autorisé")
    if any(x in text for x in ("raw_audio_missing", "raw evidence", "hash incoh", "owner incoh", "sidecar", "evidence")):
        return FailureClassification("blocked_evidence", False, "Preuve source manquante ou incohérente")
    if any(x in text for x in ("invalid json", "json output", "contract", "schema", "undeclared fields")):
        return FailureClassification("blocked_contract", False, "Sortie structurée invalide après contrôle")
    if any(x in text for x in ("module", "not installed", "absent", "no module named")):
        return FailureClassification("blocked_dependency", False, "Dépendance locale absente")
    return FailureClassification("blocked_unknown", False, "Erreur non classée : arrêt sûr sans purge")


def _backoff_values() -> tuple[float, ...]:
    values = get_settings().poststop_retry_backoff_seconds
    return values or (15.0, 60.0)


def retry_operation(
    operation: Callable[[], T],
    *,
    component: str,
    max_retries: int | None = None,
    retryable: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[int, FailureClassification, float], None] | None = None,
) -> T:
    """Run bounded retries without swallowing the final exception.

    `max_retries=2` means at most three attempts total.  The final exception is
    decorated only in its message; callers can still classify it again.
    """
    settings = get_settings()
    retry_budget = settings.poststop_retry_max if max_retries is None else max(0, int(max_retries))
    backoffs = _backoff_values()
    last_exc: BaseException | None = None
    for attempt in range(retry_budget + 1):
        try:
            return operation()
        except BaseException as exc:  # preserve original type after all attempts
            last_exc = exc
            classification = classify_failure(exc)
            should_retry = classification.retryable if retryable is None else bool(retryable(exc))
            if not should_retry or attempt >= retry_budget:
                raise
            if classification.code == "gpu_oom":
                release_gpu_memory(reason=f"{component}:gpu_oom_retry")
            delay = float(backoffs[min(attempt, len(backoffs) - 1)]) if backoffs else 0.0
            # Tiny deterministic jitter prevents a herd of local worker retries.
            delay = max(0.0, delay + min(1.0, random.Random(f"{component}:{attempt}").random()))
            if on_retry:
                on_retry(attempt + 1, classification, delay)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def gpu_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {"captured_at": now_iso(), "cuda_available": False}
    try:
        import torch
        result["cuda_available"] = bool(torch.cuda.is_available())
        if not torch.cuda.is_available():
            return result
        index = torch.cuda.current_device()
        free, total = torch.cuda.mem_get_info(index)
        result.update(
            device_index=index,
            device_name=torch.cuda.get_device_name(index),
            free_bytes=int(free),
            total_bytes=int(total),
            allocated_bytes=int(torch.cuda.memory_allocated(index)),
            reserved_bytes=int(torch.cuda.memory_reserved(index)),
        )
    except Exception as exc:
        result["error"] = str(exc)[:300]
    return result


def _runtime_log_path() -> Path:
    settings = get_settings()
    path = settings.root_dir / "runtime" / "phase_metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record_phase_event(event: str, **fields: Any) -> None:
    payload = {"event": event, "phase": runtime_phase(), "at": now_iso(), **fields, "gpu": gpu_snapshot()}
    try:
        with _runtime_log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Observability must never mask pipeline work.
        pass


def release_gpu_memory(*, reason: str) -> None:
    """Best-effort resource release between heavyweight sequential phases."""
    record_phase_event("gpu_release_started", reason=reason)
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                pass
    except Exception:
        pass
    record_phase_event("gpu_release_finished", reason=reason)


@contextlib.contextmanager
def gpu_phase(name: str, *, release_before: bool = False, release_after: bool = True) -> Iterator[None]:
    """Serial phase boundary used by post-stop orchestration."""
    with phase(name):
        if release_before:
            release_gpu_memory(reason=f"before:{name}")
        record_phase_event("phase_started")
        try:
            yield
        finally:
            record_phase_event("phase_finished")
            if release_after:
                release_gpu_memory(reason=f"after:{name}")


class DeepAudioRuntime:
    """Per-close reusable WhisperX / alignment / Pyannote runtime.

    The instance is intentionally created only around the deep-audio phase.
    It avoids model reloads for every bundle and is always explicitly closed
    before VLM/Brain2 get GPU time.
    """

    def __init__(self, *, language: str = "fr") -> None:
        self.settings = get_settings()
        self.language = language
        self._whisperx: Any | None = None
        self._models: dict[tuple[str, str, str, str], Any] = {}
        self._align: dict[tuple[str, str], tuple[Any, Any]] = {}
        self._diarizer: Any | None = None
        self._closed = False

    def whisperx_module(self) -> Any:
        if self._whisperx is None:
            try:
                import whisperx
            except Exception as exc:
                raise RuntimePolicyError("WhisperX absent", code="blocked_dependency", retryable=False, cause=exc) from exc
            self._whisperx = whisperx
        return self._whisperx

    def transcription_model(self, language: str | None = None) -> Any:
        lang = language or self.language
        key = (self.settings.whisperx_model, self.settings.whisperx_device, self.settings.whisperx_compute_type, lang)
        if key not in self._models:
            wx = self.whisperx_module()
            self._models[key] = wx.load_model(
                self.settings.whisperx_model,
                device=self.settings.whisperx_device,
                compute_type=self.settings.whisperx_compute_type,
                language=lang,
            )
            record_phase_event("deep_audio_model_loaded", model=self.settings.whisperx_model, language=lang)
        return self._models[key]

    def align_model(self, language: str) -> tuple[Any, Any]:
        key = (language, self.settings.whisperx_device)
        if key not in self._align:
            wx = self.whisperx_module()
            self._align[key] = wx.load_align_model(language_code=language, device=self.settings.whisperx_device)
            record_phase_event("deep_audio_align_loaded", language=language)
        return self._align[key]

    def diarization_model(self) -> Any:
        if not self.settings.enable_pyannote:
            return None
        if self._diarizer is None:
            if not self.settings.hf_token:
                raise RuntimePolicyError("MLOMEGA_HF_TOKEN absent", code="blocked_hf_access", retryable=False)
            wx = self.whisperx_module()
            try:
                from whisperx.diarize import DiarizationPipeline
            except Exception:
                DiarizationPipeline = getattr(wx, "DiarizationPipeline")
            try:
                self._diarizer = DiarizationPipeline(token=self.settings.hf_token, device=self.settings.whisperx_device)
            except TypeError:
                self._diarizer = DiarizationPipeline(use_auth_token=self.settings.hf_token, device=self.settings.whisperx_device)
            record_phase_event("deep_audio_diarizer_loaded")
        return self._diarizer

    def close(self) -> None:
        if self._closed:
            return
        self._models.clear()
        self._align.clear()
        self._diarizer = None
        self._whisperx = None
        self._closed = True
        release_gpu_memory(reason="deep_audio_runtime_close")

    def __enter__(self) -> "DeepAudioRuntime":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def cached_resource(key: tuple[str, ...], factory: Callable[[], T]) -> T:
    with _CACHE_LOCK:
        if key not in _PROCESS_CACHE:
            _PROCESS_CACHE[key] = factory()
        return _PROCESS_CACHE[key]


def clear_cached_resource(*prefix: str) -> None:
    with _CACHE_LOCK:
        for key in list(_PROCESS_CACHE):
            if not prefix or key[: len(prefix)] == prefix:
                _PROCESS_CACHE.pop(key, None)


def release_live_model_caches() -> None:
    """Drop models that were only useful during the real-time capture phase.

    The long-running BrainLive process moves directly into post-stop. Keeping a
    `faster-whisper` live model resident while loading WhisperX large-v3 wastes
    VRAM and is a common cause of avoidable OOM on 8–12 GB cards. The VAD and
    transient speaker-continuity caches are no longer needed after the session
    is stopped. SpeechBrain's persistent voice embedder is intentionally left
    alone because deep reconciliation may use it immediately.
    """
    cleared: list[str] = []
    try:
        from . import brainlive_sensor_fusion_v15_4 as fusion
        for attr in ("_FAST_WHISPER_CACHE", "_SILERO_VAD_CACHE", "_LIVE_SPEAKER_CACHE", "_LIVE_UNKNOWN_VOICE_CLUSTERS"):
            cache = getattr(fusion, attr, None)
            if isinstance(cache, dict):
                cache.clear()
                cleared.append(attr)
    except Exception:
        pass
    # Vector assets are created lazily later; do not retain an unrelated live
    # retrieval model through deep audio/vision.
    clear_cached_resource()
    # Ollama owns GPU memory in another process, so ``torch.empty_cache`` cannot
    # release models retained by keep_alive. At a hard live→deep boundary we
    # explicitly expire the lightweight live LLM/VLM. Deep vision and Brain2
    # reload only their own model afterwards, sequentially.
    unloaded: list[str] = []
    try:
        from .llm import ollama_unload
        settings = get_settings()
        for model in {str(settings.ollama_model or "").strip(), str(os.environ.get("MLOMEGA_VLM_MODEL") or "").strip()} - {""}:
            ollama_unload(model=model)
            unloaded.append(model)
    except Exception:
        pass
    record_phase_event("live_models_released", python_caches=cleared, ollama_models=unloaded)
    release_gpu_memory(reason="release_live_model_caches:" + ",".join(cleared or ["none"]))


def runtime_health() -> dict[str, Any]:
    with _CACHE_LOCK:
        cache_keys = ["/".join(k) for k in _PROCESS_CACHE]
    return {"phase": runtime_phase(), "cache_keys": cache_keys, "gpu": gpu_snapshot(), "at": now_iso()}
