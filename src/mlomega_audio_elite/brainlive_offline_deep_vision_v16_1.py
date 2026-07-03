from __future__ import annotations

"""V16.1 offline heavy VLM pass for BrainLive event bundles.

BrainLive must stay fast during the day, so its live VLM descriptions are short.
For silent/non-verbal life episodes, that is often not enough: an office screen,
a cigarette/pause scene, a desk state, a place/affordance, or a body/activity
cue can matter even when nobody speaks.

This module runs only after V15.14 has assembled full event bundles. It selects a
small set of representative keyframes per event, re-runs a heavier/offline VLM
on those image files, stores detailed observations, optionally materializes them
as Brain2 context turns, and makes them available to V16.0 silent-life mining.

Important contract:
- no live-loop latency impact;
- no reconstruction of a conversation;
- no psychological certainty from images;
- VLM output is system visual evidence, not user speech;
- default max keyframes per bundle is 12, sampled across the event timeline.
"""

import base64
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import get_settings
from .db import connect, init_db, upsert
from .llm import EliteLLMError, ollama_generate, ollama_unload
from .runtime_v18_7 import classify_failure, gpu_phase, record_phase_event
from .utils import json_dumps, json_loads, now_iso, stable_id

VERSION = "16.1.1-v18.8.1-evidence-connected"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_deep_vision_runs_v161(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  model TEXT,
  max_keyframes_per_bundle INTEGER DEFAULT 12,
  scanned_bundles INTEGER DEFAULT 0,
  selected_keyframes INTEGER DEFAULT 0,
  analyzed_keyframes INTEGER DEFAULT 0,
  appended_brain2_turns INTEGER DEFAULT 0,
  status TEXT NOT NULL,
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_deep_vision_observations_v161(
  deep_observation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  bundle_id TEXT NOT NULL,
  live_session_id TEXT,
  conversation_id TEXT,
  frame_id TEXT,
  image_path TEXT NOT NULL,
  frame_time TEXT,
  sample_index INTEGER DEFAULT 0,
  sample_reason TEXT,
  model TEXT,
  status TEXT NOT NULL,
  scene_summary_detailed TEXT,
  observed_activity TEXT,
  activity_confidence REAL DEFAULT 0.0,
  location_hint TEXT,
  spatial_layout TEXT,
  objects_json TEXT DEFAULT '[]',
  affordances_json TEXT DEFAULT '[]',
  visible_text_json TEXT DEFAULT '[]',
  people_presence_json TEXT DEFAULT '{}',
  screens_or_devices_json TEXT DEFAULT '[]',
  posture_motion_json TEXT DEFAULT '{}',
  work_or_rest_signal_json TEXT DEFAULT '{}',
  smoking_pause_signal_json TEXT DEFAULT '{}',
  exact_visual_evidence_json TEXT DEFAULT '[]',
  uncertainty_json TEXT DEFAULT '[]',
  qwen_json TEXT DEFAULT '{}',
  latency_ms INTEGER,
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_deep_vision_brain2_exports_v161(
  export_id TEXT PRIMARY KEY,
  deep_observation_id TEXT NOT NULL,
  bundle_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bldeep161_person_date ON brainlive_deep_vision_observations_v161(person_id, package_date, bundle_id, frame_time);
CREATE INDEX IF NOT EXISTS idx_bldeep161_bundle ON brainlive_deep_vision_observations_v161(bundle_id, status);
CREATE INDEX IF NOT EXISTS idx_bldeep161_conv ON brainlive_deep_vision_brain2_exports_v161(conversation_id, status);
"""

DEEP_VISION_SCHEMA_HINT: dict[str, Any] = {
    "scene_summary_detailed": "detailed visible description, no mind-reading",
    "observed_activity": "computer_work|phone_use|smoking_pause|walking|resting|waiting|social_presence|travel|household|unknown",
    "activity_confidence": 0.0,
    "location_hint": "visible/probable place only",
    "spatial_layout": "short spatial layout",
    "objects": ["visible object names"],
    "affordances": [
        {"label": "shade|bench|quiet_corner|screen|desk|door|path|wall|seat|other", "position_hint": "left/right/front/...", "why_relevant_visually": "visible property only", "confidence": 0.0}
    ],
    "visible_text": ["text visible in image, if any"],
    "people_presence": {"people_count": 0, "known_identity_visible": False, "notes": "uncertain unless explicit"},
    "screens_or_devices": [{"type": "computer|phone|tablet|other", "visible_content_summary": "only visible text/layout", "confidence": 0.0}],
    "posture_motion": {"visible_posture": "", "movement_hint": "", "confidence": 0.0},
    "work_or_rest_signal": {"signal": "work|rest|ambiguous|none", "visual_evidence": [], "confidence": 0.0},
    "smoking_pause_signal": {"signal": "smoking_visible|pause_outside_possible|none|ambiguous", "visual_evidence": [], "confidence": 0.0},
    "exact_visual_evidence": ["short exact visual observations from the image"],
    "uncertainty": ["what cannot be concluded"],
    "confidence": 0.0,
}


def ensure_deep_vision_schema() -> None:
    init_db()
    with connect() as con:
        con.executescript(SCHEMA)
        con.commit()


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _rows(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _safe_json(v: Any, default: Any) -> Any:
    if isinstance(v, (dict, list)):
        return v
    return json_loads(v if isinstance(v, str) else None, default)


def _clip(value: Any, n: int = 1600) -> str:
    s = str(value or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _clamp(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except Exception:
        v = default
    return max(0.0, min(1.0, v))


def _image_exists(path: str | None) -> bool:
    if not path:
        return False
    try:
        return Path(path).expanduser().exists()
    except Exception:
        return False


def _package_day(package_date: str | None) -> str:
    from .brainlive_event_assembler_v15_14 import _period_bounds
    return _period_bounds(package_date)[0]


def _transcript_chars(bundle: dict[str, Any]) -> int:
    turns = _safe_json(bundle.get("transcript_json"), []) or []
    return sum(len(str(t.get("text") or "")) for t in turns if isinstance(t, dict))


def _rehydrate_frame_paths(timeline: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Recover physical frame evidence from SQLite for legacy/compact bundles.

    This makes deep vision robust even if a prior assembler version compacted a
    raw timeline before carrying image_path into the bundle JSON.
    """
    ids = sorted({str(v.get("frame_id")) for v in timeline if isinstance(v, dict) and v.get("frame_id")})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with connect() as con:
        rows = _rows(con, f"SELECT frame_id,image_path,image_sha256,metadata_json,captured_at FROM vision_frames WHERE frame_id IN ({placeholders})", tuple(ids))
    return {str(row["frame_id"]): row for row in rows}


def _keyframe_candidates(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deduplicated raw-pixel candidates from an event bundle.

    Bundles persist their own image path in V18.8.1.  We still rehydrate from
    ``vision_frames`` as a backward-compatible integrity guard for V18.8 bundles
    assembled before the evidence-link fix.
    """
    timeline = [v for v in (_safe_json(bundle.get("vision_timeline_json"), []) or []) if isinstance(v, dict)]
    frame_rows = _rehydrate_frame_paths(timeline)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, v in enumerate(timeline):
        frame_id = str(v.get("frame_id") or "").strip() or None
        hydrated = frame_rows.get(frame_id or "", {})
        image_path = str(v.get("image_path") or hydrated.get("image_path") or "").strip()
        dedupe = image_path or frame_id or json_dumps(v)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append({
            "bundle_id": bundle.get("bundle_id"),
            "live_session_id": bundle.get("live_session_id"),
            "conversation_id": bundle.get("brain2_conversation_id"),
            "frame_id": frame_id,
            "image_path": image_path,
            "frame_time": v.get("time") or hydrated.get("captured_at"),
            "live_summary": v.get("summary"),
            "location_hint": v.get("location_hint"),
            "objects": v.get("objects"),
            "affordances": v.get("affordances"),
            "possible_user_activities": v.get("possible_user_activities"),
            "index": idx,
            "exists": _image_exists(image_path),
        })
    return out

def select_keyframes_for_bundle(bundle: dict[str, Any], *, max_keyframes: int = 12, silent_bias: bool = True) -> list[dict[str, Any]]:
    """Select representative keyframes across the event.

    Default: up to 12 frames per bundle.  We sample first/middle/last by evenly
    spaced index so long silent events do not send thousands of near-duplicates
    to the offline VLM.  Missing image files are excluded because Qwen-VL needs
    raw pixels, not only text.
    """
    candidates = [c for c in _keyframe_candidates(bundle) if c.get("exists")]
    if not candidates:
        return []
    max_keyframes = max(1, int(max_keyframes or 12))
    if len(candidates) <= max_keyframes:
        selected = candidates
    else:
        last = len(candidates) - 1
        indices = sorted({round(i * last / (max_keyframes - 1)) for i in range(max_keyframes)}) if max_keyframes > 1 else [0]
        selected = [candidates[i] for i in indices]
    for i, item in enumerate(selected):
        item["sample_index"] = i
        item["sample_reason"] = "evenly_spaced_keyframe_for_offline_deep_vlm"
    return selected


def _deep_vlm_json(image_path: str, *, model: str | None, timeout: float, personal_context: dict[str, Any] | None = None, num_predict: int = 900) -> dict[str, Any]:
    settings = get_settings()
    if not settings.enable_ollama:
        raise EliteLLMError("MLOMEGA_ENABLE_OLLAMA=false: VLM offline requis pour analyse visuelle profonde.")
    p = Path(image_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    chosen_model = model or os.environ.get("MLOMEGA_OFFLINE_VLM_MODEL") or os.environ.get("MLOMEGA_VLM_HEAVY_MODEL") or os.environ.get("MLOMEGA_VLM_MODEL") or settings.ollama_model
    image_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    system = (
        "Tu es le VLM offline lourd de Brain2/BrainLive. Tu analyses une keyframe d'un événement de vie. "
        "Décris précisément ce qui est visible: activité possible, lieu, objets, écran/appareil, posture/mouvement visible, affordances. "
        "Interdiction de deviner l'état psychologique ou l'intention interne. Si tu proposes travail/repos/pause/cigarette, cite uniquement les indices visuels. "
        "Cette sortie peut devenir preuve mémoire; sois prudent, détaillé, et marque l'incertitude. JSON strict uniquement."
    )
    prompt = json_dumps({
        "mission": "Analyse offline détaillée d'une image BrainLive pour épisodes sans parole et routines de vie.",
        "personal_context_light": personal_context or {},
        "must_separate": ["visible action", "possible activity", "hypothetical need/mood must NOT be inferred here"],
        "examples_of_use": ["ordinateur/travail", "pause dehors/cigarette", "détente", "lieu récurrent", "ombre/banc/coin calme"],
        "rules": [
            "ne pas reconstruire de conversation",
            "ne pas conclure l'humeur depuis l'image seule",
            "visible object/affordance only",
            "cite exact_visual_evidence",
            "si doute: observed_activity=unknown ou confidence basse",
        ],
    })
    payload = {
        "model": chosen_model,
        "prompt": f"SYSTEM:\n{system}\n\nUSER:\n{prompt}\n\nReturn strict JSON only.\n\nExpected shape:\n{json.dumps(DEEP_VISION_SCHEMA_HINT, ensure_ascii=False)}",
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": int(num_predict or 900)},
    }
    outer = ollama_generate(
        payload,
        timeout=max(float(timeout), settings.poststop_vlm_timeout_s),
        component="post_stop_deep_vision",
        poststop_min_timeout_s=settings.poststop_vlm_timeout_s,
    )
    data = json.loads(outer.get("response", "{}"))
    if not isinstance(data, dict):
        raise EliteLLMError("Réponse VLM offline JSON non-objet.")
    data["_model"] = chosen_model
    return data


def _normalize_observation(raw: dict[str, Any]) -> dict[str, Any]:
    affs = raw.get("affordances") or []
    if isinstance(affs, dict):
        affs = [affs]
    objs = raw.get("objects") or []
    if isinstance(objs, str):
        objs = [objs]
    visible = raw.get("visible_text") or []
    if isinstance(visible, str):
        visible = [visible]
    evidence = raw.get("exact_visual_evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    uncertainty = raw.get("uncertainty") or []
    if isinstance(uncertainty, str):
        uncertainty = [uncertainty]
    return {
        "scene_summary_detailed": _clip(raw.get("scene_summary_detailed") or raw.get("scene_summary") or raw.get("summary"), 2500),
        "observed_activity": _clip(raw.get("observed_activity") or raw.get("activity") or "unknown", 160),
        "activity_confidence": _clamp(raw.get("activity_confidence") if raw.get("activity_confidence") is not None else raw.get("confidence"), 0.0),
        "location_hint": _clip(raw.get("location_hint"), 300) if raw.get("location_hint") else None,
        "spatial_layout": _clip(raw.get("spatial_layout") or raw.get("spatial_context"), 1200) if (raw.get("spatial_layout") or raw.get("spatial_context")) else None,
        "objects": objs if isinstance(objs, list) else [],
        "affordances": affs if isinstance(affs, list) else [],
        "visible_text": visible if isinstance(visible, list) else [],
        "people_presence": raw.get("people_presence") if isinstance(raw.get("people_presence"), dict) else {},
        "screens_or_devices": raw.get("screens_or_devices") if isinstance(raw.get("screens_or_devices"), list) else [],
        "posture_motion": raw.get("posture_motion") if isinstance(raw.get("posture_motion"), dict) else {},
        "work_or_rest_signal": raw.get("work_or_rest_signal") if isinstance(raw.get("work_or_rest_signal"), dict) else {},
        "smoking_pause_signal": raw.get("smoking_pause_signal") if isinstance(raw.get("smoking_pause_signal"), dict) else {},
        "exact_visual_evidence": [str(x)[:1000] for x in evidence if x][:12],
        "uncertainty": [str(x)[:1000] for x in uncertainty if x][:10],
        "qwen_json": raw,
    }


def _fallback_from_live(candidate: dict[str, Any], error_text: str | None = None) -> dict[str, Any]:
    parts = []
    if candidate.get("live_summary"):
        parts.append(str(candidate.get("live_summary")))
    if candidate.get("location_hint"):
        parts.append("lieu=" + str(candidate.get("location_hint")))
    if candidate.get("objects"):
        parts.append("objets=" + json_dumps(candidate.get("objects")))
    if candidate.get("affordances"):
        parts.append("affordances=" + json_dumps(candidate.get("affordances")))
    if candidate.get("possible_user_activities"):
        parts.append("activites_possibles=" + json_dumps(candidate.get("possible_user_activities")))
    return {
        "scene_summary_detailed": " | ".join(parts)[:2500],
        "observed_activity": "unknown",
        "activity_confidence": 0.0,
        "location_hint": candidate.get("location_hint"),
        "spatial_layout": None,
        "objects": candidate.get("objects") if isinstance(candidate.get("objects"), list) else [],
        "affordances": candidate.get("affordances") if isinstance(candidate.get("affordances"), list) else [],
        "visible_text": [],
        "people_presence": {},
        "screens_or_devices": [],
        "posture_motion": {},
        "work_or_rest_signal": {},
        "smoking_pause_signal": {},
        "exact_visual_evidence": [x for x in parts if x][:8],
        "uncertainty": ["deep_vlm_failed: " + (error_text or "unknown_error")[:400]],
        "qwen_json": {"fallback_from_live_vision": True, "error": error_text},
    }


def run_offline_deep_vision_for_bundles(
    person_id: str = "me",
    *,
    package_date: str | None = None,
    live_session_id: str | None = None,
    model: str | None = None,
    timeout_per_image: float | None = None,
    max_keyframes_per_bundle: int = 12,
    transcript_char_threshold: int | None = None,
    limit_bundles: int = 200,
    append_to_brain2: bool = True,
    fail_on_vlm_error: bool = False,
    use_vlm: bool = True,
) -> dict[str, Any]:
    """Run the heavy/offline VLM pass over assembled event bundles.

    If transcript_char_threshold is None, all bundles with available images are
    eligible.  In the post-stop flow we run it before V16.0 silent-life mining so
    silent events can use detailed observations.  Default max images: 12 per
    bundle.
    """
    ensure_deep_vision_schema()
    from .config import get_settings
    settings = get_settings()
    timeout_per_image = float(timeout_per_image or settings.poststop_vlm_timeout_s)
    day = _package_day(package_date)
    run_id = stable_id("bldeep161run", person_id, day, now_iso(), uuid4().hex)
    now = now_iso()
    scanned = selected = analyzed = appended = 0
    status = "ok"
    error_text = None
    frame_failures: list[dict[str, Any]] = []
    chosen_model = model or os.environ.get("MLOMEGA_OFFLINE_VLM_MODEL") or os.environ.get("MLOMEGA_VLM_HEAVY_MODEL") or os.environ.get("MLOMEGA_VLM_MODEL") or get_settings().ollama_model
    try:
        with connect() as con:
            if not _table_exists(con, "brainlive_event_bundles_v1514"):
                bundles: list[dict[str, Any]] = []
            else:
                bundle_sql = "SELECT * FROM brainlive_event_bundles_v1514 WHERE person_id=? AND package_date=?"
                bundle_params: list[Any] = [person_id, day]
                if live_session_id:
                    bundle_sql += " AND live_session_id=?"
                    bundle_params.append(str(live_session_id))
                bundle_sql += " ORDER BY start_time LIMIT ?"
                bundle_params.append(int(limit_bundles))
                bundles = _rows(con, bundle_sql, tuple(bundle_params))
            scanned = len(bundles)
            # VLM is intentionally the only GPU-heavy work in this phase.  The
            # phase boundary releases WhisperX/Pyannote allocations before the
            # first frame and frees VLM allocations before Brain2 starts.
            with gpu_phase("post_stop_deep_vision", release_before=True, release_after=True):
                for b in bundles:
                    if transcript_char_threshold is not None and _transcript_chars(b) > int(transcript_char_threshold):
                        continue
                    all_candidates = _keyframe_candidates(b)
                    frames = select_keyframes_for_bundle(b, max_keyframes=max_keyframes_per_bundle)
                    # A bundle with captured visual evidence must not silently
                    # report a successful deep-vision stage with zero usable
                    # pixels. This blocks cleanup and leaves the source files for
                    # repair/resume rather than letting Brain2 proceed on a false
                    # "no visual evidence" conclusion.
                    if all_candidates and not frames:
                        frame_failures.append({
                            "bundle_id": b.get("bundle_id"),
                            "frame_id": all_candidates[0].get("frame_id"),
                            "error_code": "blocked_visual_evidence_unavailable",
                            "retryable": False,
                            "error": "bundle contains visual frames but no readable keyframe path",
                        })
                        record_phase_event("deep_vision_visual_evidence_unavailable", bundle_id=b.get("bundle_id"), frame_id=all_candidates[0].get("frame_id"))
                        continue
                    selected += len(frames)
                    for f in frames:
                        obs_id = stable_id("bldeep161", person_id, b.get("bundle_id"), f.get("frame_id") or f.get("image_path"), f.get("sample_index"), chosen_model)
                        existing = con.execute("SELECT status FROM brainlive_deep_vision_observations_v161 WHERE deep_observation_id=?", (obs_id,)).fetchone()
                        if existing and existing["status"] == "ok":
                            continue
                        started = time.time()
                        status_row = "ok"
                        row_error = None
                        raw: dict[str, Any] = {}
                        if use_vlm:
                            try:
                                raw = _deep_vlm_json(str(f.get("image_path")), model=chosen_model, timeout=timeout_per_image, personal_context={"bundle_title": b.get("title"), "place": _safe_json(b.get("place_json"), {}), "live_summary": f.get("live_summary")})
                            except Exception as exc:
                                failure = classify_failure(exc)
                                row_error = str(exc)[:1500]
                                # Store a classification at image granularity.  A later
                                # resume will skip `ok` observations and retry only this
                                # keyframe with the same deterministic observation id.
                                status_row = "retryable_error" if failure.retryable else "blocked"
                                frame_failures.append({"bundle_id": b.get("bundle_id"), "frame_id": f.get("frame_id"), "error_code": failure.code, "retryable": failure.retryable, "error": row_error})
                                record_phase_event("deep_vision_frame_failed", bundle_id=b.get("bundle_id"), frame_id=f.get("frame_id"), error_code=failure.code, retryable=failure.retryable)
                                if fail_on_vlm_error:
                                    raise
                        else:
                            row_error = "use_vlm=false"
                            status_row = "skipped_no_vlm"
                        norm = _normalize_observation(raw) if raw else _fallback_from_live(f, row_error)
                        latency_ms = int((time.time() - started) * 1000)
                        upsert(con, "brainlive_deep_vision_observations_v161", {
                            "deep_observation_id": obs_id,
                            "run_id": run_id,
                            "person_id": person_id,
                            "package_date": day,
                            "bundle_id": b.get("bundle_id"),
                            "live_session_id": b.get("live_session_id"),
                            "conversation_id": b.get("brain2_conversation_id"),
                            "frame_id": f.get("frame_id"),
                            "image_path": str(Path(str(f.get("image_path"))).expanduser()),
                            "frame_time": f.get("frame_time"),
                            "sample_index": int(f.get("sample_index") or 0),
                            "sample_reason": f.get("sample_reason"),
                            "model": chosen_model,
                            "status": status_row,
                            "scene_summary_detailed": norm["scene_summary_detailed"],
                            "observed_activity": norm["observed_activity"],
                            "activity_confidence": norm["activity_confidence"],
                            "location_hint": norm.get("location_hint"),
                            "spatial_layout": norm.get("spatial_layout"),
                            "objects_json": json_dumps(norm.get("objects") or []),
                            "affordances_json": json_dumps(norm.get("affordances") or []),
                            "visible_text_json": json_dumps(norm.get("visible_text") or []),
                            "people_presence_json": json_dumps(norm.get("people_presence") or {}),
                            "screens_or_devices_json": json_dumps(norm.get("screens_or_devices") or []),
                            "posture_motion_json": json_dumps(norm.get("posture_motion") or {}),
                            "work_or_rest_signal_json": json_dumps(norm.get("work_or_rest_signal") or {}),
                            "smoking_pause_signal_json": json_dumps(norm.get("smoking_pause_signal") or {}),
                            "exact_visual_evidence_json": json_dumps(norm.get("exact_visual_evidence") or []),
                            "uncertainty_json": json_dumps(norm.get("uncertainty") or []),
                            "qwen_json": json_dumps(norm.get("qwen_json") or {}),
                            "latency_ms": latency_ms,
                            "error_text": row_error,
                            "created_at": now_iso(),
                            "updated_at": now_iso(),
                        }, "deep_observation_id")
                        if status_row == "ok":
                            analyzed += 1
            con.commit()
        if frame_failures:
            status = "retryable_error" if all(bool(item.get("retryable")) for item in frame_failures) else "blocked"
            error_text = f"{len(frame_failures)} deep VLM keyframe(s) unresolved"
        # Do not append fallback/error observations to Brain2. The retained `ok`
        # images are durable, while unresolved images keep the post-stop stage
        # retryable and block Brain2/cleanup until they are repaired.
        if append_to_brain2 and status == "ok":
            appended = append_deep_vision_context_turns_to_brain2(person_id, package_date=day, only_status_ok=True).get("turns_appended", 0)
    except Exception as exc:
        status = "error"
        error_text = str(exc)[:2000]
        raise
    finally:
        with connect() as con:
            upsert(con, "brainlive_deep_vision_runs_v161", {
                "run_id": run_id,
                "person_id": person_id,
                "package_date": day,
                "model": chosen_model,
                "max_keyframes_per_bundle": int(max_keyframes_per_bundle or 12),
                "scanned_bundles": scanned,
                "selected_keyframes": selected,
                "analyzed_keyframes": analyzed,
                "appended_brain2_turns": appended,
                "status": status,
                "error_text": error_text,
                "created_at": now,
                "updated_at": now_iso(),
            }, "run_id")
            con.commit()
        # Qwen-VL is only needed for this phase. Do not leave it resident while
        # Brain2 and its local LLM are about to use the same GPU.
        if use_vlm:
            ollama_unload(model=chosen_model)
            record_phase_event("deep_vision_model_unloaded", model=chosen_model)
    return {"version": VERSION, "run_id": run_id, "person_id": person_id, "package_date": day, "live_session_id": live_session_id, "model": chosen_model, "max_keyframes_per_bundle": int(max_keyframes_per_bundle or 12), "scanned_bundles": scanned, "selected_keyframes": selected, "analyzed_keyframes": analyzed, "appended_brain2_turns": appended, "status": status, "failures": frame_failures, "error": error_text}


def _deep_turn_text(row: dict[str, Any]) -> str:
    parts = []
    if row.get("scene_summary_detailed"):
        parts.append(str(row.get("scene_summary_detailed")))
    if row.get("observed_activity"):
        parts.append("activité_visible=" + str(row.get("observed_activity")))
    if row.get("location_hint"):
        parts.append("lieu_probable=" + str(row.get("location_hint")))
    if row.get("spatial_layout"):
        parts.append("spatial=" + str(row.get("spatial_layout")))
    for label, col in (("objets", "objects_json"), ("affordances", "affordances_json"), ("texte_visible", "visible_text_json"), ("écrans_appareils", "screens_or_devices_json")):
        vals = _safe_json(row.get(col), [] if col.endswith("json") else {})
        if vals:
            parts.append(f"{label}=" + json_dumps(vals))
    ev = _safe_json(row.get("exact_visual_evidence_json"), [])
    if ev:
        parts.append("preuves_visuelles=" + json_dumps(ev[:8]))
    unc = _safe_json(row.get("uncertainty_json"), [])
    if unc:
        parts.append("incertitudes=" + json_dumps(unc[:6]))
    return ("[CONTEXT_VISION_DEEP] " + " | ".join(parts))[:8000]


def append_deep_vision_context_turns_to_brain2(person_id: str = "me", *, package_date: str | None = None, only_status_ok: bool = False) -> dict[str, Any]:
    """Append deep VLM observations as context turns in exported Brain2 conversations."""
    ensure_deep_vision_schema()
    day = _package_day(package_date)
    appended = 0
    with connect() as con:
        status_filter = "AND o.status='ok'" if only_status_ok else "AND o.status IN ('ok','vlm_error','skipped_no_vlm')"
        rows = _rows(con, f"""
            SELECT o.*, e.conversation_id AS exported_conversation_id
            FROM brainlive_deep_vision_observations_v161 o
            JOIN brainlive_brain2_event_exports_v1514 e ON e.bundle_id=o.bundle_id
            WHERE o.person_id=? AND o.package_date=? {status_filter}
            ORDER BY o.bundle_id, o.sample_index, o.frame_time
        """, (person_id, day))
        for r in rows:
            conv_id = r.get("exported_conversation_id") or r.get("conversation_id")
            if not conv_id:
                continue
            export_id = stable_id("bldeep161export", r.get("deep_observation_id"), conv_id)
            if con.execute("SELECT 1 FROM brainlive_deep_vision_brain2_exports_v161 WHERE export_id=?", (export_id,)).fetchone():
                continue
            max_idx_row = con.execute("SELECT COALESCE(MAX(idx), -1) AS m FROM turns WHERE conversation_id=?", (conv_id,)).fetchone()
            idx = int(max_idx_row["m"] or -1) + 1
            text = _deep_turn_text(r)
            turn_id = stable_id("turn_bldeep161", conv_id, r.get("deep_observation_id"), text)
            prev = con.execute("SELECT turn_id FROM turns WHERE conversation_id=? ORDER BY idx DESC LIMIT 1", (conv_id,)).fetchone()
            upsert(con, "turns", {
                "turn_id": turn_id,
                "conversation_id": conv_id,
                "idx": idx,
                "speaker_label": "context_vision_deep",
                "person_id": None,
                "start_s": None,
                "end_s": None,
                "text": text,
                "previous_turn_id": prev["turn_id"] if prev else None,
                "metadata_json": json_dumps({
                    "kind": "deep_vision_context",
                    "time": r.get("frame_time"),
                    "evidence_role": "system_observation_not_user_speech",
                    "brainlive_bundle_id": r.get("bundle_id"),
                    "deep_observation_id": r.get("deep_observation_id"),
                    "frame_id": r.get("frame_id"),
                    "image_path": r.get("image_path"),
                    "model": r.get("model"),
                    "status": r.get("status"),
                    "v16_1": True,
                }),
            }, "turn_id")
            upsert(con, "brainlive_deep_vision_brain2_exports_v161", {
                "export_id": export_id,
                "deep_observation_id": r.get("deep_observation_id"),
                "bundle_id": r.get("bundle_id"),
                "conversation_id": conv_id,
                "turn_id": turn_id,
                "status": "exported",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }, "export_id")
            appended += 1
        con.commit()
    return {"version": VERSION, "person_id": person_id, "package_date": day, "turns_appended": appended}


def deep_vision_audit(person_id: str = "me", *, package_date: str | None = None) -> dict[str, Any]:
    ensure_deep_vision_schema()
    day = _package_day(package_date)
    with connect() as con:
        runs = _rows(con, "SELECT * FROM brainlive_deep_vision_runs_v161 WHERE person_id=? AND package_date=? ORDER BY created_at DESC LIMIT 5", (person_id, day))
        counts = _rows(con, "SELECT status, COUNT(*) AS n FROM brainlive_deep_vision_observations_v161 WHERE person_id=? AND package_date=? GROUP BY status", (person_id, day))
        exported = _rows(con, """
            SELECT COUNT(*) AS n FROM brainlive_deep_vision_brain2_exports_v161 e
            JOIN brainlive_deep_vision_observations_v161 o ON o.deep_observation_id=e.deep_observation_id
            WHERE o.person_id=? AND o.package_date=?
        """, (person_id, day))
    return {"version": VERSION, "person_id": person_id, "package_date": day, "latest_runs": runs, "observation_counts": {r["status"]: int(r["n"]) for r in counts}, "brain2_deep_vision_turns": int(exported[0]["n"]) if exported else 0}

# V18: failed deep-VLM responses never become pseudo-dialogue evidence.
from .v18_poststop_outputs import install_deep as _install_v18_deep_outputs
_globals_v18_deep_outputs = _install_v18_deep_outputs(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_deep_outputs)
