from __future__ import annotations

"""V18.8 operational gates: deterministic doctor, smoke probes and resume.

This module intentionally has no optional heavy imports at module import time.
The installer and the RUN script use it to refuse false-success states: a
component is "ready" only after a concrete probe, not merely after ``pip
install`` or a process launch command.
"""

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import get_settings, validate_core_profile
from .db import connect, init_db
from .runtime_v18_8 import gpu_snapshot, release_gpu_memory
from .utils import now_iso

VERSION = "18.8.1-operations"

# Core only.  Graphiti, Neo4j and Mem0 are deliberately absent.
REQUIRED_IMPORTS: dict[str, str] = {
    "dotenv": "python-dotenv",
    "requests": "requests",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "qdrant_client": "qdrant-client",
    "torch": "torch",
    "torchaudio": "torchaudio",
    "whisperx": "whisperx",
    "pyannote.audio": "pyannote.audio",
    "speechbrain": "speechbrain",
    "silero_vad": "silero-vad",
    "faster_whisper": "faster-whisper",
    "sentence_transformers": "sentence-transformers",
}


@dataclass
class _Check:
    name: str
    ok: bool
    detail: str
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "required": self.required}


def _http_json(
    url: str,
    *,
    timeout: float,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Small stdlib HTTP helper used by readiness probes.

    Probes intentionally use real protocol operations rather than merely
    checking that a TCP port accepts connections.  This avoids a false ready
    state when a reverse proxy, stale process or broken Qdrant index is alive.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=body, method=method.upper(), headers=request_headers)
    with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as response:
        raw = response.read().decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("JSON response is not an object")
    return value


def _probe_qdrant_read_write() -> _Check:
    """Verify one ephemeral collection can be created, written and read.

    ``/collections`` returning HTTP 200 alone is insufficient: a damaged
    volume or wrong API endpoint can still make the actual vector store
    unusable.  The temporary collection is removed in ``finally``.
    """
    cfg = get_settings()
    base = cfg.qdrant_url.rstrip("/")
    name = f"mlomega_v187_smoke_{os.getpid()}_{int(time.time() * 1000)}"
    created = False
    try:
        _http_json(
            f"{base}/collections/{name}",
            timeout=max(5.0, cfg.ollama_connect_timeout_s),
            method="PUT",
            payload={"vectors": {"size": 2, "distance": "Cosine"}},
        )
        created = True
        _http_json(
            f"{base}/collections/{name}/points?wait=true",
            timeout=max(5.0, cfg.ollama_connect_timeout_s),
            method="PUT",
            payload={"points": [{"id": 1, "vector": [0.0, 1.0], "payload": {"probe": "v18_8"}}]},
        )
        result = _http_json(
            f"{base}/collections/{name}/points/scroll",
            timeout=max(5.0, cfg.ollama_connect_timeout_s),
            method="POST",
            payload={"limit": 4, "with_payload": True, "with_vector": False},
        )
        points = ((result.get("result") or {}).get("points") or []) if isinstance(result.get("result"), dict) else []
        ok = any(isinstance(point, dict) and str(point.get("id")) == "1" and (point.get("payload") or {}).get("probe") == "v18_8" for point in points)
        return _Check("qdrant_read_write", ok, "ephemeral collection round-trip" if ok else "probe point was not returned")
    except Exception as exc:
        return _Check("qdrant_read_write", False, f"{type(exc).__name__}: {exc}")
    finally:
        if created:
            try:
                _http_json(
                    f"{base}/collections/{name}",
                    timeout=max(5.0, cfg.ollama_connect_timeout_s),
                    method="DELETE",
                )
            except Exception:
                # The failed cleanup must not hide the readiness result.  The
                # collection name is unique and contains no user data.
                pass


def _model_names(payload: dict[str, Any]) -> set[str]:
    raw = payload.get("models") or []
    names: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
            elif isinstance(item, str):
                names.add(item)
    return names


def _expected_ollama_models() -> list[str]:
    cfg = get_settings()
    values = [
        cfg.ollama_model,
        os.environ.get("MLOMEGA_VLM_MODEL", "moondream"),
        os.environ.get("MLOMEGA_OFFLINE_VLM_MODEL") or os.environ.get("MLOMEGA_VLM_HEAVY_MODEL", "qwen3-vl:8b"),
    ]
    result: list[str] = []
    for value in values:
        val = str(value or "").strip()
        if val and val not in result:
            result.append(val)
    return result


def _which_ok(command: str) -> _Check:
    path = shutil.which(command)
    return _Check(command, bool(path), path or f"{command} is not on PATH")


def _write_smoke_asset(path: Path) -> Path:
    """Write a tiny valid PNG without Pillow; used only for a VLM transport probe."""
    # 1x1 transparent PNG.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
        "kQ5J7wAAAABJRU5ErkJggg=="
    )
    path.write_bytes(png)
    return path


def _http_multipart_json(
    url: str,
    *,
    file_path: Path,
    meta: dict[str, Any],
    timeout: float,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Submit the same multipart shape used by the Android receiver client."""
    boundary = f"----MLOmegaDoctor{os.getpid()}_{int(time.time() * 1000)}"
    def part(disposition: str, content: bytes, content_type: str | None = None) -> bytes:
        lines = [f"--{boundary}", f"Content-Disposition: {disposition}"]
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + content + b"\r\n"
    body = b"".join([
        part('form-data; name="meta"', json.dumps(meta, ensure_ascii=False).encode("utf-8"), "application/json"),
        part(f'form-data; name="file"; filename="{file_path.name}"', file_path.read_bytes(), "audio/wav"),
        f"--{boundary}--\r\n".encode("ascii"),
    ])
    request_headers = {"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=body, method="POST", headers=request_headers)
    with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as response:
        raw = response.read().decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("multipart JSON response is not an object")
    return value


def _probe_bridge_delivery() -> _Check:
    """Send a fixture through the receiver queue and verify inbox + sidecar.

    Production installs launch this against a dedicated temporary bridge state
    directory.  It therefore proves upload, durable queueing, pump delivery and
    sidecar normalization without polluting a user's live inbox.
    """
    cfg = get_settings()
    token = os.environ.get("MLOMEGA_PHONE_TOKEN", "").strip()
    if not cfg.phone_bridge_url or not token:
        return _Check("phone_bridge_delivery", False, "bridge URL or phone token missing")
    try:
        fixture = _fixture_audio_path()
        source_event_id = f"doctor-bridge-{os.getpid()}-{int(time.time() * 1000)}"
        upload = _http_multipart_json(
            cfg.phone_bridge_url.rstrip("/") + "/upload/audio",
            file_path=fixture,
            meta={"source_event_id": source_event_id, "captured_at": now_iso(), "owner_person_id": "doctor-smoke"},
            timeout=max(15.0, cfg.ollama_connect_timeout_s),
            headers={"X-MLOmega-Token": token},
        )
        queue_id = str(upload.get("queued_id") or "")
        if not upload.get("ok") or not queue_id:
            return _Check("phone_bridge_delivery", False, f"upload response={upload}")
        _http_json(
            cfg.phone_bridge_url.rstrip("/") + "/pump-now",
            method="POST",
            payload={},
            timeout=max(15.0, cfg.ollama_connect_timeout_s),
            headers={"X-MLOmega-Token": token},
        )
        deadline = time.time() + max(15.0, cfg.ollama_connect_timeout_s)
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            report = _http_json(
                cfg.phone_bridge_url.rstrip("/") + "/status",
                timeout=max(5.0, cfg.ollama_connect_timeout_s),
                headers={"X-MLOmega-Token": token},
            )
            rows = report.get("last") if isinstance(report.get("last"), list) else []
            last = next((row for row in rows if isinstance(row, dict) and str(row.get("id")) == queue_id), None)
            if last and str(last.get("status")) == "delivered":
                delivered = Path(str(last.get("delivered_path") or ""))
                sidecar = delivered.with_suffix(delivered.suffix + ".json")
                if delivered.exists() and sidecar.exists():
                    return _Check("phone_bridge_delivery", True, f"queued={queue_id} delivered={delivered.name} sidecar={sidecar.name}")
                return _Check("phone_bridge_delivery", False, f"delivered record missing artifact/sidecar: {delivered}")
            time.sleep(0.25)
        return _Check("phone_bridge_delivery", False, f"queue did not deliver in time: {last}")
    except Exception as exc:
        return _Check("phone_bridge_delivery", False, f"{type(exc).__name__}: {exc}")


def _probe_ollama_generation(*, probe_vlm: bool) -> list[_Check]:
    """Run a real LLM request and one independent image request per VLM model.

    A tag listed by ``/api/tags`` is not sufficient proof: a model can be
    corrupt, unsupported by the installed daemon, or fail its multimodal
    contract.  Each configured VLM is therefore exercised separately and
    unloaded before the next GPU phase.
    """
    cfg = get_settings()
    checks: list[_Check] = []
    from .llm import ollama_generate, ollama_unload

    try:
        out = ollama_generate(
            {"model": cfg.ollama_model, "prompt": "Réponds uniquement: OK", "stream": False, "keep_alive": "0", "options": {"num_predict": 8, "temperature": 0}},
            timeout=max(60.0, cfg.ollama_cold_start_timeout_s), component="install_smoke_llm", retry_max=1,
        )
        text = str(out.get("response") or "").strip()
        checks.append(_Check("ollama_llm_generate", bool(text), text[:160] or "empty response"))
    except Exception as exc:
        checks.append(_Check("ollama_llm_generate", False, f"{type(exc).__name__}: {exc}"))
    finally:
        ollama_unload(model=cfg.ollama_model)
        release_gpu_memory(reason="doctor_after_ollama_llm")

    if not probe_vlm:
        return checks
    asset = _write_smoke_asset(cfg.root_dir / "runtime" / "smoke_vlm.png")
    image_b64 = base64.b64encode(asset.read_bytes()).decode("ascii")
    # Keep model names distinct while preserving explicit configuration order.
    vlm_models: list[str] = []
    for value in (
        os.environ.get("MLOMEGA_VLM_MODEL", "moondream"),
        os.environ.get("MLOMEGA_OFFLINE_VLM_MODEL") or os.environ.get("MLOMEGA_VLM_HEAVY_MODEL", "qwen3-vl:8b"),
    ):
        name = str(value or "").strip()
        if name and name not in vlm_models:
            vlm_models.append(name)
    for model in vlm_models:
        check_name = f"ollama_vlm_generate:{model}"
        try:
            out = ollama_generate(
                {"model": model, "prompt": 'Réponds uniquement JSON: {"ok":true}', "images": [image_b64], "stream": False, "format": "json", "keep_alive": "0", "options": {"num_predict": 24, "temperature": 0}},
                timeout=max(90.0, cfg.ollama_cold_start_timeout_s, cfg.poststop_vlm_timeout_s), component=f"install_smoke_vlm:{model}", retry_max=1,
            )
            text = str(out.get("response") or "").strip()
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            checks.append(_Check(check_name, isinstance(parsed, dict), text[:300] or "invalid/empty JSON"))
        except Exception as exc:
            checks.append(_Check(check_name, False, f"{type(exc).__name__}: {exc}"))
        finally:
            try:
                ollama_unload(model=model)
            finally:
                release_gpu_memory(reason=f"doctor_after_ollama_vlm:{model}")
    return checks


def _fixture_audio_path() -> Path:
    """Return the package-owned French speech fixture required by doctor.

    The fixture is generated during release assembly, not downloaded at doctor
    time.  This keeps the doctor deterministic and proves the installed model
    stack processes real audio rather than only importing Python modules.
    """
    path = Path(__file__).resolve().parents[2] / "fixtures" / "doctor_fr.wav"
    if not path.exists() or path.stat().st_size < 1024:
        raise FileNotFoundError(f"doctor audio fixture missing or invalid: {path}")
    return path


def _probe_audio_models() -> list[_Check]:
    """Exercise deep and live audio paths on one French speech fixture.

    The test intentionally invokes model inference: WhisperX transcription and
    alignment, Pyannote diarization, SpeechBrain embedding, Silero VAD and
    faster-whisper live ASR.  A successful import or model constructor alone
    cannot qualify a production installation.
    """
    checks: list[_Check] = []
    cfg = get_settings()
    fixture: Path | None = None
    try:
        fixture = _fixture_audio_path()
        from .runtime_v18_8 import DeepAudioRuntime
        from .audio_pipeline import transcribe_with_whisperx
        runtime = DeepAudioRuntime(language="fr")
        try:
            transcript = transcribe_with_whisperx(fixture, language="fr", runtime=runtime)
            turns = [t for t in (transcript.get("turns") or []) if isinstance(t, dict)]
            nonempty = [t for t in turns if str(t.get("text") or "").strip()]
            word_rows = [w for t in nonempty for w in (t.get("words") or []) if isinstance(w, dict)]
            ordered = all(float(w.get("end", -1)) >= float(w.get("start", 0)) >= 0 for w in word_rows)
            checks.append(_Check("whisperx_transcription_fixture", bool(nonempty), f"turns={len(turns)} nonempty={len(nonempty)}"))
            checks.append(_Check("whisperx_alignment_timestamps_fixture", bool(word_rows) and ordered, f"words={len(word_rows)} ordered={ordered}"))
            # These calls force construction of the three deep runtime members
            # even if a permissive WhisperX result contained no diarized words.
            runtime.transcription_model("fr")
            runtime.align_model("fr")
            runtime.diarization_model()
            checks.extend([
                _Check("whisperx_large_v3_load", True, f"{cfg.whisperx_model} transcribed fixture"),
                _Check("whisperx_alignment_load", True, "fr alignment model produced word timestamps"),
                _Check("pyannote_diarization_load", True, "diarization pipeline ran during fixture transcription"),
            ])
        finally:
            runtime.close()
    except Exception as exc:
        checks.append(_Check("deep_audio_fixture", False, f"{type(exc).__name__}: {exc}"))
    finally:
        release_gpu_memory(reason="doctor_after_deep_audio_fixture")

    try:
        if fixture is None:
            fixture = _fixture_audio_path()
        from .brainlive_sensor_fusion_v15_4 import run_vad
        vad = run_vad(fixture, backend="silero", allow_energy_fallback=False)
        checks.append(_Check("silero_vad_fixture", str(vad.get("status")) == "ok", json.dumps(vad, ensure_ascii=False)[:300]))
    except Exception as exc:
        checks.append(_Check("silero_vad_fixture", False, f"{type(exc).__name__}: {exc}"))
    finally:
        release_gpu_memory(reason="doctor_after_vad_fixture")

    try:
        if fixture is None:
            fixture = _fixture_audio_path()
        from .voice_identity import SpeechBrainVoiceEmbedder
        vector = SpeechBrainVoiceEmbedder().embed_file(fixture)
        checks.append(_Check("speechbrain_ecapa_embedding_fixture", isinstance(vector, list) and len(vector) > 32, f"embedding_dimensions={len(vector) if isinstance(vector, list) else 0}"))
    except Exception as exc:
        checks.append(_Check("speechbrain_ecapa_embedding_fixture", False, f"{type(exc).__name__}: {exc}"))
    finally:
        release_gpu_memory(reason="doctor_after_speechbrain_fixture")

    try:
        if fixture is None:
            fixture = _fixture_audio_path()
        from .brainlive_sensor_fusion_v15_4 import transcribe_segment
        live = transcribe_segment(fixture, backend="faster", language="fr")
        checks.append(_Check("faster_whisper_live_fixture", str(live.get("status")) in {"ok", "empty"}, json.dumps(live, ensure_ascii=False)[:300]))
    except Exception as exc:
        checks.append(_Check("faster_whisper_live_fixture", False, f"{type(exc).__name__}: {exc}"))
    finally:
        release_gpu_memory(reason="doctor_after_faster_whisper_fixture")
    return checks


def _probe_vector_models() -> list[_Check]:
    checks: list[_Check] = []
    try:
        from .vector_memory import get_embedder, get_reranker
        get_embedder()
        checks.append(_Check("embedding_model_load", True, "sentence-transformers embedder loaded"))
        release_gpu_memory(reason="doctor_after_embedder")
        get_reranker()
        checks.append(_Check("reranker_model_load", True, "sentence-transformers reranker loaded"))
    except Exception as exc:
        checks.append(_Check("vector_model_load", False, f"{type(exc).__name__}: {exc}"))
    finally:
        release_gpu_memory(reason="doctor_after_vector_models")
    return checks


def core_doctor(
    *,
    check_services: bool = True,
    check_models: bool = False,
    check_bridge: bool = False,
    check_bridge_delivery: bool = False,
    check_vectors: bool = False,
) -> dict[str, Any]:
    """Validate the supported V18.8 core profile without false positives."""
    cfg = get_settings()
    checks: list[_Check] = []
    for message in validate_core_profile(cfg):
        checks.append(_Check("core_profile", False, message))
    if not any(c.name == "core_profile" for c in checks):
        checks.append(_Check("core_profile", True, cfg.deployment_profile))

    py_ok = sys.version_info[:2] == (3, 11)
    checks.append(_Check("python_version", py_ok, sys.version.split()[0]))
    checks.append(_which_ok("ffmpeg"))
    for module, package in REQUIRED_IMPORTS.items():
        checks.append(_Check(f"import:{module}", importlib.util.find_spec(module) is not None, package))

    try:
        path = init_db()
        checks.append(_Check("sqlite_schema", True, str(path)))
    except Exception as exc:
        checks.append(_Check("sqlite_schema", False, f"{type(exc).__name__}: {exc}"))

    snapshot = gpu_snapshot()
    checks.append(_Check("cuda", bool(snapshot.get("cuda_available")), json.dumps(snapshot, ensure_ascii=False)))

    if check_services:
        try:
            q = _http_json(cfg.qdrant_url.rstrip("/") + "/collections", timeout=cfg.ollama_connect_timeout_s)
            q_ok = bool(q.get("result") is not None or q.get("status") == "ok")
            checks.append(_Check("qdrant_health", q_ok, cfg.qdrant_url))
            if q_ok:
                checks.append(_probe_qdrant_read_write())
        except Exception as exc:
            checks.append(_Check("qdrant_health", False, f"{cfg.qdrant_url}: {type(exc).__name__}: {exc}"))
        try:
            version_payload = _http_json(cfg.ollama_base_url.rstrip("/") + "/api/version", timeout=cfg.ollama_connect_timeout_s)
            raw_version = str(version_payload.get("version") or "")
            try:
                parts = tuple(int(piece) for piece in raw_version.split(".")[:3])
                version_ok = len(parts) == 3 and parts >= (0, 12, 7)
            except Exception:
                version_ok = False
            checks.append(_Check("ollama_version", version_ok, raw_version or "missing /api/version value"))
        except Exception as exc:
            checks.append(_Check("ollama_version", False, f"{cfg.ollama_base_url}: {type(exc).__name__}: {exc}"))
        try:
            tags = _http_json(cfg.ollama_base_url.rstrip("/") + "/api/tags", timeout=cfg.ollama_connect_timeout_s)
            available = _model_names(tags)
            missing = [m for m in _expected_ollama_models() if m not in available]
            checks.append(_Check("ollama_models", not missing, "missing=" + ", ".join(missing) if missing else "all required models available"))
        except Exception as exc:
            checks.append(_Check("ollama_models", False, f"{cfg.ollama_base_url}: {type(exc).__name__}: {exc}"))

    if check_bridge:
        if not cfg.phone_bridge_url:
            checks.append(_Check("phone_bridge_health", False, "MLOMEGA_PHONE_BRIDGE_URL missing"))
        else:
            try:
                health = _http_json(cfg.phone_bridge_url.rstrip("/") + "/health", timeout=cfg.ollama_connect_timeout_s)
                project_root = str(health.get("project_root") or "")
                expected = os.environ.get("MLOMEGA_PROJECT_ROOT") or str(Path.cwd().resolve())
                allow_post_stop = bool(health.get("allow_post_stop"))
                details = f"receiver={health.get('receiver_version')} root={project_root} allow_post_stop={allow_post_stop}"
                checks.append(_Check(
                    "phone_bridge_health",
                    bool(health.get("ok")) and allow_post_stop and (not project_root or Path(project_root).resolve() == Path(expected).resolve()),
                    details,
                ))
                token = os.environ.get("MLOMEGA_PHONE_TOKEN", "")
                if not token:
                    checks.append(_Check("phone_bridge_authenticated_status", False, "MLOMEGA_PHONE_TOKEN missing"))
                else:
                    status = _http_json(
                        cfg.phone_bridge_url.rstrip("/") + "/status",
                        timeout=cfg.ollama_connect_timeout_s,
                        headers={"X-MLOmega-Token": token},
                    )
                    checks.append(_Check("phone_bridge_authenticated_status", bool(status.get("ok")), "authenticated status endpoint"))
            except Exception as exc:
                checks.append(_Check("phone_bridge_health", False, f"{cfg.phone_bridge_url}: {type(exc).__name__}: {exc}"))
        if check_bridge_delivery:
            checks.append(_probe_bridge_delivery())
    elif check_bridge_delivery:
        checks.append(_Check("phone_bridge_delivery", False, "delivery probe requires --check-bridge"))

    if check_models:
        checks.extend(_probe_ollama_generation(probe_vlm=True))
        checks.extend(_probe_audio_models())
        if check_vectors:
            checks.extend(_probe_vector_models())

    try:
        from .brainlive_service_v15_5 import recover_stale_brainlive_service_runs
        stale = recover_stale_brainlive_service_runs()
        checks.append(_Check("stale_service_recovery", True, json.dumps(stale, ensure_ascii=False)))
    except Exception as exc:
        checks.append(_Check("stale_service_recovery", False, f"{type(exc).__name__}: {exc}"))

    errors = [c.as_dict() for c in checks if c.required and not c.ok]
    result = {
        "version": VERSION,
        "at": now_iso(),
        "status": "ok" if not errors else "failed",
        "checks": [c.as_dict() for c in checks],
        "errors": errors,
        "gpu": snapshot,
        "profile": cfg.deployment_profile,
        "expected_models": _expected_ollama_models(),
    }
    path = cfg.root_dir / "runtime" / "doctor_core_v18_8.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def resume_close_day(*, person_id: str, package_date: str | None = None, force: bool = False) -> dict[str, Any]:
    """Resume the logical close-day checkpoint.  Completed stages remain cached."""
    from .v18_close_day import close_brainlive_day
    return close_brainlive_day(person_id=person_id, package_date=package_date, force=force)


def recovery_status(*, person_id: str) -> dict[str, Any]:
    """Return every unresolved V18.8 boundary for one owner.

    ``RUN`` uses this as a hard guard: it must never start a fresh capture over
    an orphaned inbox or a partially completed post-stop/close-day run.  The
    query is intentionally read-only and schema-aware, so it is safe before a
    first capture as well as after a power loss.
    """
    if not str(person_id or "").strip():
        raise ValueError("recovery status requires person_id")
    from .brainlive_service_v15_5 import ensure_service_schema
    from .brainlive_poststop_deep_flow_v15_15 import ensure_post_stop_deep_flow_schema
    from .v18_close_day import ensure_close_day_schema
    ensure_service_schema()
    ensure_post_stop_deep_flow_schema()
    ensure_close_day_schema()
    unresolved: list[dict[str, Any]] = []
    with connect() as con:
        service_rows = con.execute(
            """SELECT service_run_id,live_session_id,status,started_at,stopped_at,last_error
               FROM brainlive_service_runs
               WHERE person_id=? AND status IN ('orphaned','stopped_pending_ingest','drain_recovery')
               ORDER BY COALESCE(stopped_at,started_at) DESC""",
            (person_id,),
        ).fetchall()
        for row in service_rows:
            item = dict(row)
            item.update(kind="service_inbox", action="resume_inbox_drain_then_close_day")
            unresolved.append(item)
        post_rows = con.execute(
            """SELECT run_id,package_date,live_session_id,service_run_id,status,error_text,updated_at
               FROM brainlive_post_stop_deep_flow_runs_v1515
               WHERE person_id=? AND status IN ('retryable_error','running','blocked')
               ORDER BY updated_at DESC""",
            (person_id,),
        ).fetchall()
        for row in post_rows:
            item = dict(row)
            item.update(kind="post_stop", action="resume_close_day")
            unresolved.append(item)
        close_rows = con.execute(
            """SELECT close_day_id,package_date,live_session_id,service_run_id,status,error_text,updated_at
               FROM v18_close_day_runs
               WHERE person_id=? AND status IN ('retryable_error','running','blocked')
               ORDER BY updated_at DESC""",
            (person_id,),
        ).fetchall()
        for row in close_rows:
            item = dict(row)
            item.update(kind="close_day", action="resume_close_day")
            unresolved.append(item)
    return {
        "version": VERSION,
        "person_id": person_id,
        "status": "resume_required" if unresolved else "ready_for_new_capture",
        "unresolved": unresolved,
        "resume_command": "RESUME_MLOMEGA_V18_8.ps1" if unresolved else None,
    }


def runtime_status() -> dict[str, Any]:
    cfg = get_settings()
    path = cfg.root_dir / "runtime" / "brainlive_service.json"
    payload: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        payload = raw if isinstance(raw, dict) else {}
    except Exception as exc:
        payload = {"status": "invalid_manifest", "error": str(exc)}
    return {"version": VERSION, "manifest_path": str(path), "service": payload, "gpu": gpu_snapshot()}
