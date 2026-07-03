from __future__ import annotations

"""V15.2 BrainLive real-time perception bridge.

This layer connects the already-created BrainLive/Brain2 memory stack to live
inputs. It does not replace Brain2 and it does not infer psychology with regex.
It turns current signals into structured perception snapshots, then calls the
BrainLive LLM with horizon-specific budgets:

- H0: immediate, narrow, actionability-first target (<~2s on fast local setup)
- H1: tactical, conversation/situation target (<~5s)
- H2: broader short-term trajectory target (<~12s)

Important honesty contract:
- voice/person recognition requires enrolled SpeechBrain ECAPA profiles or an
  explicit speaker_person_id from the upstream ASR/diarization pipeline;
- place recognition requires vision/GPS/location_hint or an LLM/VLM observation;
- no regex/keyword psychology is used. Without model/captor evidence, fields are
  explicitly marked unknown/llm_required instead of guessed.
"""

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .brainlive_v15 import (
    build_active_context,
    ensure_brainlive_schema,
    ingest_live_turn,
    ingest_vision_frame,
    run_brainlive,
    start_live_session,
)
from .config import get_settings
from .db import connect, init_db, upsert
from .llm import OllamaJsonClient, EliteLLMError, ollama_generate
from .utils import json_dumps, json_loads, now_iso, sha256_file, stable_id

VERSION = "15.2.0-realtime-perception-bridge-no-keyword-psychology"

REALTIME_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_realtime_ticks(
  tick_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  horizon TEXT NOT NULL,
  status TEXT NOT NULL,
  target_latency_ms INTEGER,
  observed_latency_ms INTEGER,
  input_refs_json TEXT DEFAULT '{}',
  perception_snapshot_id TEXT,
  analysis_run_id TEXT,
  result_json TEXT DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_perception_snapshots(
  perception_snapshot_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  snapshot_time TEXT NOT NULL,
  horizon TEXT NOT NULL,
  active_people_json TEXT DEFAULT '[]',
  speaker_resolution_json TEXT DEFAULT '{}',
  location_resolution_json TEXT DEFAULT '{}',
  speech_context_json TEXT DEFAULT '{}',
  vision_context_json TEXT DEFAULT '{}',
  active_context_id TEXT,
  readiness_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_vlm_runs(
  vlm_run_id TEXT PRIMARY KEY,
  frame_id TEXT,
  live_session_id TEXT,
  image_path TEXT,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  qwen_json TEXT DEFAULT '{}',
  latency_ms INTEGER,
  error_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_person_presence(
  presence_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT,
  label TEXT,
  source TEXT NOT NULL,
  confidence REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '{}',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS brainlive_place_presence(
  place_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  place_label TEXT,
  source TEXT NOT NULL,
  confidence REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '{}',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS brainlive_runtime_profiles(
  profile_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  h0_timeout_s REAL DEFAULT 2.0,
  h1_timeout_s REAL DEFAULT 5.0,
  h2_timeout_s REAL DEFAULT 12.0,
  h0_limit INTEGER DEFAULT 8,
  h1_limit INTEGER DEFAULT 16,
  h2_limit INTEGER DEFAULT 32,
  vlm_timeout_s REAL DEFAULT 8.0,
  active_context_refresh_s REAL DEFAULT 15.0,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bl_rt_ticks_session ON brainlive_realtime_ticks(live_session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_rt_snap_session ON brainlive_perception_snapshots(live_session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_presence_session ON brainlive_person_presence(live_session_id, status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_bl_place_session ON brainlive_place_presence(live_session_id, status, last_seen_at);
"""

VISION_SCHEMA = {
    "scene_summary": "",
    "location_hint": "",
    "people_count": 0,
    "spatial_context": "",
    "social_context_hint": "",
    "visible_text": [],
    "objects": [],
    "risks": [],
    "affordances": [
        {"label": "", "world_element": "", "position_hint": "", "why_relevant": "", "confidence": 0.0}
    ],
    "possible_user_activities": [
        {"activity": "", "confidence": 0.0, "evidence": []}
    ],
    "personal_relevance": {"items": []},
    "confidence": 0.0,
}

HORIZON_CONFIG = {
    "H0": {"timeout": 2.0, "limit": 8, "mode": "h0_reactive", "target_latency_ms": 2000},
    "H1": {"timeout": 5.0, "limit": 16, "mode": "h1_tactical", "target_latency_ms": 5000},
    "H2": {"timeout": 12.0, "limit": 32, "mode": "h2_strategic", "target_latency_ms": 12000},
}


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = _one(con, "SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1")
    return str(row["person_id"]) if row and row.get("person_id") else "me"


def _clamp(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except Exception:
        v = default
    return max(0.0, min(1.0, v))


def ensure_realtime_schema() -> None:
    ensure_brainlive_schema()
    init_db()
    with connect() as con:
        con.executescript(REALTIME_SCHEMA)
        con.commit()


class OllamaVisionJsonClient:
    """Strict local VLM JSON client for Ollama multimodal models.

    Uses /api/generate with base64 images. It intentionally has no deterministic
    vision fallback; if the VLM is unavailable, the run is recorded as failed.
    """

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        if not settings.enable_ollama:
            raise EliteLLMError("MLOMEGA_ENABLE_OLLAMA=false: VLM Ollama requis pour vision live.")
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or __import__("os").environ.get("MLOMEGA_VLM_MODEL", settings.ollama_model)

    def require_json_for_image(self, image_path: str | Path, system: str, prompt: str, schema_hint: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        p = Path(image_path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        image_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "prompt": f"SYSTEM:\n{system}\n\nUSER:\n{prompt}\n\nReturn strict JSON only.",
            "images": [image_b64],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_predict": 256},
        }
        if schema_hint:
            payload["prompt"] += "\n\nExpected shape:\n" + json.dumps(schema_hint, ensure_ascii=False)
        # Route live VLM traffic through the same transport as Brain2/deep
        # vision so keep_alive, observability and error classification are
        # consistent.  Live perception stays latency-bounded: no long post-stop
        # retry backoff is allowed in the hot path; failed frames are retained
        # and can still be analysed by the offline deep-VLM stage.
        outer = ollama_generate(
            payload,
            base_url=self.base_url,
            timeout=timeout,
            component="live_vlm",
            retry_max=0,
        )
        data = json.loads(outer.get("response", "{}"))
        if not isinstance(data, dict):
            raise EliteLLMError("Réponse VLM JSON non-objet.")
        return data


def configure_runtime_profile(*, person_id: str | None = None, h0_timeout: float = 2.0, h1_timeout: float = 5.0, h2_timeout: float = 12.0, vlm_timeout: float = 8.0) -> dict[str, Any]:
    if not person_id:
        from .governance_v18 import ScopeError
        raise ScopeError("BrainLive runtime profile requires explicit person_id")
    ensure_realtime_schema()
    now = now_iso()
    with connect() as con:
        pid = stable_id("blrtprofile", person_id)
        upsert(con, "brainlive_runtime_profiles", {
            "profile_id": pid,
            "person_id": person_id,
            "h0_timeout_s": float(h0_timeout),
            "h1_timeout_s": float(h1_timeout),
            "h2_timeout_s": float(h2_timeout),
            "h0_limit": 8,
            "h1_limit": 16,
            "h2_limit": 32,
            "vlm_timeout_s": float(vlm_timeout),
            "active_context_refresh_s": 15.0,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }, "profile_id")
        con.commit()
    return {"profile_id": pid, "person_id": person_id, "h0_timeout": h0_timeout, "h1_timeout": h1_timeout, "h2_timeout": h2_timeout, "vlm_timeout": vlm_timeout}


def _runtime_profile(con, person_id: str) -> dict[str, Any]:
    row = _one(con, "SELECT * FROM brainlive_runtime_profiles WHERE person_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1", (person_id,))
    if row:
        return row
    return {"h0_timeout_s": 2.0, "h1_timeout_s": 5.0, "h2_timeout_s": 12.0, "h0_limit": 8, "h1_limit": 16, "h2_limit": 32, "vlm_timeout_s": 8.0}


def analyze_vision_with_vlm(image_path: str | Path, *, live_session_id: str | None = None, model: str | None = None, timeout: float = 8.0, personal_context: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_realtime_schema()
    now = now_iso()
    run_id = stable_id("blvlm", str(image_path), live_session_id or "none", now, uuid4().hex)
    status = "ok"
    error_text = None
    q: dict[str, Any] = {}
    started = time.time()
    try:
        system = (
            "Tu es le VLM BrainLive. Décris uniquement ce qui est visible et ce qui peut être utile pour le modèle personnel. "
            "Ne devine pas la psychologie. Identifie personnes/objets/texte/lieu probable/affordances visibles. JSON strict."
        )
        prompt = json_dumps({
            "mission": "Analyser cette image pour enrichir BrainLive: lieu, personnes visibles, objets, texte, risques, affordances visibles et pertinence personnelle potentielle.",
            "personal_context": personal_context or {},
            "rules": ["no psychological inference from image alone", "mark uncertainty", "no violence instructions"],
        })
        q = OllamaVisionJsonClient(model=model).require_json_for_image(image_path, system, prompt, schema_hint=VISION_SCHEMA, timeout=timeout)
    except Exception as exc:
        status = "vlm_error"
        error_text = str(exc)[:2000]
    latency_ms = int((time.time() - started) * 1000)
    with connect() as con:
        upsert(con, "brainlive_vlm_runs", {
            "vlm_run_id": run_id,
            "frame_id": None,
            "live_session_id": live_session_id,
            "image_path": str(Path(image_path).expanduser().resolve()),
            "model": model or __import__("os").environ.get("MLOMEGA_VLM_MODEL", get_settings().ollama_model),
            "status": status,
            "qwen_json": json_dumps(q),
            "latency_ms": latency_ms,
            "error_text": error_text,
            "created_at": now,
        }, "vlm_run_id")
        con.commit()
    return {"vlm_run_id": run_id, "status": status, "latency_ms": latency_ms, "observation": q, "error_text": error_text}


def ingest_live_image(live_session_id: str, image_path: str | Path, *, device_source: str | None = None, use_vlm: bool = True, model: str | None = None, timeout: float | None = None) -> dict[str, Any]:
    ensure_realtime_schema()
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        profile = _runtime_profile(con, sess["person_id"])
    observation: dict[str, Any] | None = None
    vlm_result: dict[str, Any] | None = None
    if use_vlm:
        ctx = build_active_context(live_session_id, limit=8).get("context", {})
        vlm_result = analyze_vision_with_vlm(image_path, live_session_id=live_session_id, model=model, timeout=timeout or float(profile.get("vlm_timeout_s") or 8.0), personal_context={"active_context_light": {"session": ctx.get("session"), "recent_turns_summary": ctx.get("recent_turns_summary"), "routines": ctx.get("brainlive_routine_cards", [])[:5]}})
        observation = vlm_result.get("observation") if vlm_result.get("status") == "ok" else None
    ingested = ingest_vision_frame(image_path, live_session_id=live_session_id, device_source=device_source, observation=observation, model=model or "ollama_vlm" if observation else "captured_no_vlm")
    if vlm_result:
        with connect() as con:
            con.execute("UPDATE brainlive_vlm_runs SET frame_id=? WHERE vlm_run_id=?", (ingested.get("frame_id"), vlm_result.get("vlm_run_id")))
            con.commit()
        ingested["vlm"] = vlm_result
    return ingested


def resolve_active_speaker(*, audio_sample_path: str | None = None, explicit_person_id: str | None = None, speaker_label: str | None = None, explicit_confidence: float | None = None) -> dict[str, Any]:
    """Resolve active speaker without text heuristics.

    Priority: explicit upstream identity -> SpeechBrain voice match -> unknown label.
    """
    if explicit_person_id:
        return {"person_id": explicit_person_id, "label": speaker_label or explicit_person_id, "confidence": _clamp(explicit_confidence if explicit_confidence is not None else 1.0), "source": "explicit_upstream"}
    if audio_sample_path:
        try:
            from .voice_identity import match_voice
            m = match_voice(Path(audio_sample_path))
            return {"person_id": m.get("person_id") if m.get("matched") else None, "label": speaker_label or (m.get("person_id") if m.get("matched") else "unknown_voice"), "confidence": _clamp(m.get("score")), "source": "speechbrain_voice_match", "raw": m}
        except Exception as exc:
            return {"person_id": None, "label": speaker_label or "unknown_voice", "confidence": 0.0, "source": "voice_match_error", "error": str(exc)[:500]}
    return {"person_id": None, "label": speaker_label or "unknown_speaker", "confidence": _clamp(explicit_confidence), "source": "unresolved"}


def _record_presence(live_session_id: str, resolution: dict[str, Any], *, kind: str = "person") -> None:
    now = now_iso()
    with connect() as con:
        if kind == "person":
            pid = resolution.get("person_id")
            label = resolution.get("label") or pid or "unknown_speaker"
            presence_id = stable_id("blpresence", live_session_id, pid or label, resolution.get("source"))
            upsert(con, "brainlive_person_presence", {
                "presence_id": presence_id,
                "live_session_id": live_session_id,
                "person_id": pid,
                "label": label,
                "source": resolution.get("source") or "unknown",
                "confidence": _clamp(resolution.get("confidence")),
                "evidence_json": json_dumps(resolution),
                "first_seen_at": now,
                "last_seen_at": now,
                "status": "active",
            }, "presence_id")
        elif kind == "place":
            label = resolution.get("place_label") or resolution.get("location_hint") or "unknown_place"
            place_id = stable_id("blplace", live_session_id, label, resolution.get("source"))
            upsert(con, "brainlive_place_presence", {
                "place_id": place_id,
                "live_session_id": live_session_id,
                "place_label": label,
                "source": resolution.get("source") or "unknown",
                "confidence": _clamp(resolution.get("confidence")),
                "evidence_json": json_dumps(resolution),
                "first_seen_at": now,
                "last_seen_at": now,
                "status": "active",
            }, "place_id")
        con.commit()


def resolve_place_from_session(live_session_id: str, *, explicit_location: str | None = None) -> dict[str, Any]:
    ensure_realtime_schema()
    if explicit_location:
        return {"place_label": explicit_location, "location_hint": explicit_location, "confidence": 1.0, "source": "explicit_location"}
    with connect() as con:
        sess = _one(con, "SELECT active_location_hint FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,)) or {}
        if sess.get("active_location_hint"):
            return {"place_label": sess.get("active_location_hint"), "location_hint": sess.get("active_location_hint"), "confidence": 0.8, "source": "session_location_hint"}
        obs = _one(con, "SELECT location_hint, confidence, scene_summary FROM vision_scene_observations WHERE live_session_id=? AND location_hint IS NOT NULL AND location_hint!='' ORDER BY created_at DESC LIMIT 1", (live_session_id,))
        if obs:
            return {"place_label": obs.get("location_hint"), "location_hint": obs.get("location_hint"), "confidence": _clamp(obs.get("confidence"), 0.5), "source": "vision_location_hint", "evidence": obs}
    return {"place_label": None, "location_hint": None, "confidence": 0.0, "source": "unresolved"}


def build_perception_snapshot(live_session_id: str, *, horizon: str = "H1", speaker_resolution: dict[str, Any] | None = None, location_resolution: dict[str, Any] | None = None, active_context_id: str | None = None) -> dict[str, Any]:
    ensure_realtime_schema()
    horizon = horizon.upper()
    now = now_iso()
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        person_id = sess["person_id"]
        active_people_rows = _many(con, "SELECT * FROM brainlive_person_presence WHERE live_session_id=? AND status='active' ORDER BY confidence DESC, last_seen_at DESC LIMIT 10", (live_session_id,))
        active_people = []
        for r in active_people_rows:
            active_people.append({"person_id": r.get("person_id"), "label": r.get("label"), "confidence": r.get("confidence"), "source": r.get("source")})
        turns = _many(con, "SELECT * FROM brainlive_turn_buffer WHERE live_session_id=? ORDER BY created_at DESC LIMIT 12", (live_session_id,))
        visions = _many(con, "SELECT * FROM vision_scene_observations WHERE live_session_id=? ORDER BY created_at DESC LIMIT 5", (live_session_id,))
        place = location_resolution or resolve_place_from_session(live_session_id)
        if place.get("place_label") or place.get("location_hint"):
            _record_presence(live_session_id, place, kind="place")
        readiness = {
            "has_recent_speech": bool(turns),
            "has_recent_vision": bool(visions),
            "has_active_people": bool(active_people),
            "has_place": bool(place.get("place_label") or place.get("location_hint")),
            "active_context_preloaded": bool(active_context_id),
            "horizon": horizon,
        }
        sid = stable_id("blsnap", live_session_id, horizon, now, uuid4().hex)
        upsert(con, "brainlive_perception_snapshots", {
            "perception_snapshot_id": sid,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "snapshot_time": now,
            "horizon": horizon,
            "active_people_json": json_dumps(active_people),
            "speaker_resolution_json": json_dumps(speaker_resolution or {}),
            "location_resolution_json": json_dumps(place),
            "speech_context_json": json_dumps({"recent_turns": turns}),
            "vision_context_json": json_dumps({"recent_observations": visions}),
            "active_context_id": active_context_id,
            "readiness_json": json_dumps(readiness),
            "created_at": now,
        }, "perception_snapshot_id")
        con.commit()
    return {"perception_snapshot_id": sid, "person_id": person_id, "readiness": readiness, "active_people": active_people, "location": place}


def live_tick(
    live_session_id: str,
    *,
    horizon: str = "H1",
    text: str | None = None,
    image_path: str | None = None,
    audio_sample_path: str | None = None,
    speaker_label: str | None = None,
    speaker_person_id: str | None = None,
    speaker_confidence: float | None = None,
    location_hint: str | None = None,
    use_vlm: bool = True,
    use_llm: bool = True,
    timeout: float | None = None,
) -> dict[str, Any]:
    """One complete BrainLive live cycle.

    It connects speech/person/place/vision to Brain2 context and runs one H0/H1/H2
    analysis. This is the operational flow the project was missing.
    """
    ensure_realtime_schema()
    horizon = horizon.upper()
    if horizon not in HORIZON_CONFIG:
        raise ValueError("horizon must be H0, H1 or H2")
    started = time.time()
    input_refs: dict[str, Any] = {}
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        person_id = sess["person_id"]
        profile = _runtime_profile(con, person_id)
    cfg = HORIZON_CONFIG[horizon].copy()
    if timeout is None:
        timeout = float(profile.get(f"{horizon.lower()}_timeout_s") or cfg["timeout"])
    speaker = resolve_active_speaker(audio_sample_path=audio_sample_path, explicit_person_id=speaker_person_id, speaker_label=speaker_label, explicit_confidence=speaker_confidence)
    _record_presence(live_session_id, speaker, kind="person")
    if text:
        turn = ingest_live_turn(
            live_session_id,
            text,
            speaker_label=speaker.get("label"),
            speaker_person_id=speaker.get("person_id"),
            speaker_confidence=_clamp(speaker.get("confidence")),
            is_final=True,
            metadata={"source": "brainlive_realtime_tick", "horizon": horizon, "speaker_resolution": speaker},
        )
        input_refs["live_turn_id"] = turn.get("live_turn_id")
    if image_path:
        img = ingest_live_image(live_session_id, image_path, use_vlm=use_vlm, timeout=min(float(profile.get("vlm_timeout_s") or 8.0), max(timeout, 1.0)))
        input_refs["frame_id"] = img.get("frame_id")
        input_refs["vision"] = img
    place = resolve_place_from_session(live_session_id, explicit_location=location_hint)
    if place.get("place_label") or place.get("location_hint"):
        _record_presence(live_session_id, place, kind="place")
    # Preload Brain2/BrainLive context after ingesting current signals.
    active_people = []
    if speaker.get("person_id"):
        active_people.append(speaker["person_id"])
    ctx = build_active_context(live_session_id, active_people=active_people or None, limit=int(profile.get(f"{horizon.lower()}_limit") or cfg["limit"]))
    snap = build_perception_snapshot(live_session_id, horizon=horizon, speaker_resolution=speaker, location_resolution=place, active_context_id=ctx.get("active_context_id"))
    tick_id = stable_id("bltick", live_session_id, horizon, now_iso(), uuid4().hex)
    analysis: dict[str, Any] | None = None
    status = "ok"
    error_text = None
    try:
        analysis = run_brainlive(live_session_id, mode=str(cfg["mode"]), use_llm=use_llm, timeout=timeout, active_people=active_people or None, limit=int(profile.get(f"{horizon.lower()}_limit") or cfg["limit"]))
        if analysis.get("status") not in {"ok", "llm_required"}:
            status = str(analysis.get("status"))
            error_text = analysis.get("error_text")
    except Exception as exc:
        status = "error"
        error_text = str(exc)[:2000]
    latency_ms = int((time.time() - started) * 1000)
    with connect() as con:
        upsert(con, "brainlive_realtime_ticks", {
            "tick_id": tick_id,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "horizon": horizon,
            "status": status,
            "target_latency_ms": int(cfg["target_latency_ms"]),
            "observed_latency_ms": latency_ms,
            "input_refs_json": json_dumps(input_refs),
            "perception_snapshot_id": snap.get("perception_snapshot_id"),
            "analysis_run_id": (analysis or {}).get("run_id"),
            "result_json": json_dumps({"analysis": analysis, "snapshot": snap}),
            "error_text": error_text,
            "created_at": now_iso(),
        }, "tick_id")
        con.commit()
    return {"tick_id": tick_id, "status": status, "horizon": horizon, "target_latency_ms": int(cfg["target_latency_ms"]), "observed_latency_ms": latency_ms, "latency_ok": latency_ms <= int(cfg["target_latency_ms"]), "snapshot": snap, "analysis": analysis, "error_text": error_text}


def live_cycle_all_horizons(live_session_id: str, *, text: str | None = None, image_path: str | None = None, audio_sample_path: str | None = None, speaker_label: str | None = None, speaker_person_id: str | None = None, location_hint: str | None = None, use_vlm: bool = True, use_llm: bool = True) -> dict[str, Any]:
    """Run H0, H1, H2 sequentially from one signal packet.

    H0 gets the fresh signal. H1/H2 use already-ingested state, avoiding duplicate
    transcript/image rows while still reusing the same active context stack.
    """
    h0 = live_tick(live_session_id, horizon="H0", text=text, image_path=image_path, audio_sample_path=audio_sample_path, speaker_label=speaker_label, speaker_person_id=speaker_person_id, location_hint=location_hint, use_vlm=use_vlm, use_llm=use_llm)
    h1 = live_tick(live_session_id, horizon="H1", use_vlm=False, use_llm=use_llm)
    h2 = live_tick(live_session_id, horizon="H2", use_vlm=False, use_llm=use_llm)
    return {"live_session_id": live_session_id, "H0": h0, "H1": h1, "H2": h2}


def realtime_audit() -> dict[str, Any]:
    ensure_realtime_schema()
    tables = ["brainlive_realtime_ticks", "brainlive_perception_snapshots", "brainlive_vlm_runs", "brainlive_person_presence", "brainlive_place_presence", "brainlive_runtime_profiles"]
    with connect() as con:
        counts = {t: int(con.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]) for t in tables}
    return {"version": VERSION, "status": "ok", "counts": counts, "latency_targets": {"H0_ms": 2000, "H1_ms": 5000, "H2_ms": 12000}, "contract": "No regex/keyword psychology. Unknown if missing LLM/VLM/voice/place evidence."}

# V18 remediation: one validated inference for a signal packet; horizons are
# forecast windows within that inference, not three competing LLM calls.
from .v18_live_execution import install_realtime as _install_v18_realtime
_globals_v18_realtime = _install_v18_realtime(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_realtime)
