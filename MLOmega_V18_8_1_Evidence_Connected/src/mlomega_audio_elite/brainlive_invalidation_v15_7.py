from __future__ import annotations

"""V15.7 BrainLive intelligent cache invalidation.

The previous hot loop made the right architectural move (active identity + hot
Brain2 context) but still refreshed too aggressively when a meaningful signal
arrived. This module adds the missing production behaviour:

- keep identity/place/vision/context hot and stable while the same situation is
  unfolding;
- invalidate only the layer that changed (speaker, place, visual scene, topic,
  or TTL), not the whole BrainLive stack;
- let semantic topic-change detection be LLM-only when available, never regex;
- use deterministic fingerprints only for sensor/state equality, not for
  psychological interpretation;
- keep a trace of every reuse/refresh decision so latency regressions are
  auditable.

Brain2 remains the deep truth. BrainLive keeps short-lived projections and
invalidates them when the present diverges from the active state.
"""

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .brainlive_hotloop_v15_6 import (
    VERSION as HOT_VERSION,
    _clamp,
    _latest_identity_cache,
    ensure_hotloop_schema,
    prepare_hot_context,
    resolve_speaker_hot,
    route_triggers_llm,
    run_unified_hot_prediction,
)
from .brainlive_sensor_fusion_v15_4 import build_fused_situation, resolve_place_multisource
from .brainlive_v15 import ensure_brainlive_schema
from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id

VERSION = "15.7.0-intelligent-invalidation"

INVALIDATION_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_invalidation_state(
  live_session_id TEXT PRIMARY KEY,
  identity_key TEXT,
  identity_json TEXT DEFAULT '{}',
  place_key TEXT,
  place_json TEXT DEFAULT '{}',
  visual_key TEXT,
  visual_json TEXT DEFAULT '{}',
  topic_key TEXT,
  topic_json TEXT DEFAULT '{}',
  active_context_key TEXT,
  active_context_id TEXT,
  context_expires_at_epoch REAL,
  last_fused_id TEXT,
  last_budget_run_id TEXT,
  turn_count INTEGER DEFAULT 0,
  stable_turns INTEGER DEFAULT 0,
  changed_turns INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_invalidation_decisions(
  decision_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  decision_json TEXT NOT NULL,
  sensor_keys_json TEXT DEFAULT '{}',
  reuse_json TEXT DEFAULT '{}',
  refresh_json TEXT DEFAULT '{}',
  latency_budget_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blinv_decisions_session ON brainlive_invalidation_decisions(live_session_id, created_at);
"""


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def ensure_invalidation_schema() -> None:
    ensure_brainlive_schema()
    ensure_hotloop_schema()
    init_db()
    with connect() as con:
        con.executescript(INVALIDATION_SCHEMA)
        con.commit()


def _hash_obj(prefix: str, obj: Any) -> str:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return stable_id(prefix, text[:8000])


def _latest_turn_text(con, live_session_id: str, limit: int = 8) -> list[dict[str, Any]]:
    # Schema variants in the project have compatible names in V15 additions.
    if not _table_exists(con, "brainlive_turn_buffer"):
        return []
    rows = _many(
        con,
        "SELECT live_turn_id,text_final,text_partial,speaker_person_id,speaker_label,created_at FROM brainlive_turn_buffer WHERE live_session_id=? ORDER BY created_at DESC LIMIT ?",
        (live_session_id, limit),
    )
    rows.reverse()
    return rows


def _latest_visual(con, live_session_id: str) -> dict[str, Any]:
    # V15.4 normalized observations.
    if _table_exists(con, "brainlive_vlm_observations_v154"):
        row = _one(con, "SELECT * FROM brainlive_vlm_observations_v154 WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,))
        if row:
            return {"source": "v154_vlm", "created_at": row.get("created_at"), "normalized": json_loads(row.get("normalized_json"), {}), "confidence": row.get("confidence")}
    if _table_exists(con, "vision_scene_observations"):
        row = _one(con, "SELECT * FROM vision_scene_observations WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,))
        if row:
            return {"source": "vision_scene_observations", "created_at": row.get("created_at"), "summary": row.get("scene_summary"), "location_hint": row.get("location_hint"), "objects": json_loads(row.get("objects_json"), [])}
    return {}


def _latest_state(con, live_session_id: str) -> dict[str, Any] | None:
    return _one(con, "SELECT * FROM brainlive_invalidation_state WHERE live_session_id=?", (live_session_id,))


def _sensor_keys(
    live_session_id: str,
    *,
    explicit_location: str | None = None,
    gps_json: dict[str, Any] | None = None,
    explicit_speaker_person_id: str | None = None,
    latest_speaker_label: str | None = None,
) -> dict[str, Any]:
    with connect() as con:
        identity_cache = _latest_identity_cache(con, live_session_id) if _table_exists(con, "brainlive_hot_identity_cache") else None
        visual = _latest_visual(con, live_session_id)
        turns = _latest_turn_text(con, live_session_id, limit=8)
    # Canonical identity key: stable when the same person remains active.
    # Do not include cache confidence/status in the key, otherwise the first
    # verification would falsely look like a speaker change.
    canonical_person_id = explicit_speaker_person_id or (identity_cache or {}).get("person_id")
    identity_payload = {
        "person_id": canonical_person_id,
        "label": latest_speaker_label or (identity_cache or {}).get("label") or canonical_person_id,
    }
    place_payload = {
        "explicit_location": explicit_location,
        "gps": gps_json or {},
        "visual_location": (visual.get("normalized") or {}).get("location_hint") or visual.get("location_hint"),
    }
    # Keep visual key stable across identical observations. We intentionally do
    # not infer meaning here; this is just equality/change detection.
    norm = visual.get("normalized") or visual
    visual_payload = {
        "source": visual.get("source"),
        "location_hint": norm.get("location_hint") if isinstance(norm, dict) else None,
        "people_count": norm.get("people_count") if isinstance(norm, dict) else None,
        "objects": norm.get("objects") or norm.get("visible_objects") if isinstance(norm, dict) else None,
        "spatial": norm.get("spatial_context") if isinstance(norm, dict) else None,
    }
    topic_payload = {
        "turn_ids": [t.get("live_turn_id") for t in turns[-4:]],
        "text": "\n".join(str(t.get("text_final") or t.get("text_partial") or "").strip() for t in turns[-4:])[-2000:],
        "speaker_ids": [t.get("speaker_person_id") for t in turns[-4:] if t.get("speaker_person_id")],
    }
    return {
        "identity": identity_payload,
        "identity_key": _hash_obj("identkey", identity_payload),
        "place": place_payload,
        "place_key": _hash_obj("placekey", place_payload),
        "visual": visual_payload,
        "visual_key": _hash_obj("visualkey", visual_payload),
        "topic": topic_payload,
        "topic_key": _hash_obj("topickey", topic_payload),
    }


def _llm_topic_change(previous_topic: dict[str, Any], current_topic: dict[str, Any], *, timeout: float = 2.5) -> dict[str, Any]:
    """Semantic topic drift, LLM-only.

    If unavailable, return unresolved rather than guessing from keywords.
    """
    prev_text = str(previous_topic.get("text") or "").strip()
    cur_text = str(current_topic.get("text") or "").strip()
    if not cur_text:
        return {"status": "no_current_text", "changed": False, "confidence": 0.0}
    if not prev_text:
        return {"status": "first_topic", "changed": True, "confidence": 0.7, "reason": "no previous topic"}
    try:
        out = OllamaJsonClient().require_json(
            "Tu es BrainLive Topic Drift. Tu détectes seulement si le sujet/situation conversationnelle a changé assez pour rafraîchir le contexte actif. Pas de psychologie, pas de mots-clés: compare sémantiquement les deux fenêtres. JSON strict.",
            json_dumps({"previous_window": previous_topic, "current_window": current_topic, "decision": "Does this require refreshing Brain2 active context or only continue?"}),
            schema_hint={"changed": False, "change_level": "none|minor|major", "confidence": 0.0, "why": "", "new_topic_summary": "", "refresh_needed": "none|light|topic"},
            timeout=timeout,
        )
        return {"status": "ok", **out, "changed": bool(out.get("changed") or out.get("refresh_needed") in {"light", "topic"})}
    except Exception as exc:
        return {"status": "llm_unavailable", "changed": False, "confidence": 0.0, "error": str(exc)[:800], "refresh_needed": "none"}


def decide_invalidation(
    live_session_id: str,
    *,
    explicit_location: str | None = None,
    gps_json: dict[str, Any] | None = None,
    explicit_speaker_person_id: str | None = None,
    latest_speaker_label: str | None = None,
    meaningful_signal: bool = True,
    force_context: bool = False,
) -> dict[str, Any]:
    """Decide what to reuse/recompute for this loop.

    Rules are about cache/state only. They do not infer needs/emotions.
    """
    ensure_invalidation_schema()
    started = time.time()
    keys = _sensor_keys(
        live_session_id,
        explicit_location=explicit_location,
        gps_json=gps_json,
        explicit_speaker_person_id=explicit_speaker_person_id,
        latest_speaker_label=latest_speaker_label,
    )
    with connect() as con:
        prev = _latest_state(con, live_session_id)
    now_epoch = time.time()
    prev_identity = json_loads((prev or {}).get("identity_json"), {}) if prev else {}
    prev_place = json_loads((prev or {}).get("place_json"), {}) if prev else {}
    prev_visual = json_loads((prev or {}).get("visual_json"), {}) if prev else {}
    prev_topic = json_loads((prev or {}).get("topic_json"), {}) if prev else {}

    identity_changed = not prev or (prev.get("identity_key") != keys["identity_key"] and bool(explicit_speaker_person_id or keys["identity"].get("person_id")))
    place_changed = not prev or prev.get("place_key") != keys["place_key"]
    visual_changed = not prev or prev.get("visual_key") != keys["visual_key"]
    topic_key_changed = not prev or prev.get("topic_key") != keys["topic_key"]

    topic_drift = {"status": "not_checked", "changed": False, "confidence": 0.0, "refresh_needed": "none"}
    if meaningful_signal and topic_key_changed:
        topic_drift = _llm_topic_change(prev_topic, keys["topic"], timeout=2.5)

    context_expired = not prev or float(prev.get("context_expires_at_epoch") or 0.0) <= now_epoch
    # Refresh levels: identity/place => full; topic => light/topic; visual => vision only unless place changed.
    if force_context or identity_changed or place_changed:
        context_refresh = "full"
    elif context_expired:
        context_refresh = "ttl"
    elif topic_drift.get("refresh_needed") in {"light", "topic"} or topic_drift.get("changed"):
        context_refresh = "topic"
    elif visual_changed and meaningful_signal:
        context_refresh = "vision_only"
    else:
        context_refresh = "none"

    recompute = {
        "identity": bool(identity_changed or force_context),
        "place": bool(place_changed or force_context),
        "vision": bool(visual_changed),
        "topic": bool(topic_drift.get("changed") or topic_key_changed),
        "brain2_context": context_refresh,
    }
    reuse = {
        "identity": not recompute["identity"],
        "place": not recompute["place"],
        "vision": not recompute["vision"],
        "brain2_context": context_refresh == "none",
        "previous_budget_run_id": (prev or {}).get("last_budget_run_id") if prev else None,
        "previous_fused_id": (prev or {}).get("last_fused_id") if prev else None,
    }
    decision = {
        "version": VERSION,
        "live_session_id": live_session_id,
        "meaningful_signal": meaningful_signal,
        "identity_changed": identity_changed,
        "place_changed": place_changed,
        "visual_changed": visual_changed,
        "topic_key_changed": topic_key_changed,
        "topic_drift": topic_drift,
        "context_expired": context_expired,
        "context_refresh": context_refresh,
        "recompute": recompute,
        "reuse": reuse,
        "latency_ms": int((time.time() - started) * 1000),
    }
    decision_id = stable_id("blinvdec", live_session_id, now_iso(), uuid4().hex)
    with connect() as con:
        upsert(con, "brainlive_invalidation_decisions", {
            "decision_id": decision_id,
            "live_session_id": live_session_id,
            "decision_json": json_dumps(decision),
            "sensor_keys_json": json_dumps(keys),
            "reuse_json": json_dumps(reuse),
            "refresh_json": json_dumps(recompute),
            "latency_budget_json": json_dumps({"target_total_ms": 12000, "decision_ms": decision["latency_ms"]}),
            "created_at": now_iso(),
        }, "decision_id")
        con.commit()
    decision["decision_id"] = decision_id
    decision["sensor_keys"] = keys
    return decision


def _save_state(
    live_session_id: str,
    *,
    decision: dict[str, Any],
    speaker: dict[str, Any] | None,
    place: dict[str, Any] | None,
    hot_context: dict[str, Any] | None,
    fused_id: str | None,
    budget_run_id: str | None,
    context_ttl_s: float = 45.0,
) -> None:
    keys = decision.get("sensor_keys") or {}
    prev_stable = 0
    with connect() as con:
        prev = _latest_state(con, live_session_id)
        if prev:
            prev_stable = int(prev.get("stable_turns") or 0)
    unchanged = decision.get("context_refresh") == "none" and not decision.get("identity_changed") and not decision.get("place_changed")
    row = {
        "live_session_id": live_session_id,
        "identity_key": keys.get("identity_key"),
        "identity_json": json_dumps(speaker or keys.get("identity") or {}),
        "place_key": keys.get("place_key"),
        "place_json": json_dumps(place or keys.get("place") or {}),
        "visual_key": keys.get("visual_key"),
        "visual_json": json_dumps(keys.get("visual") or {}),
        "topic_key": keys.get("topic_key"),
        "topic_json": json_dumps(keys.get("topic") or {}),
        "active_context_key": (hot_context or {}).get("active_context_id") or (hot_context or {}).get("active_context_key"),
        "active_context_id": (hot_context or {}).get("active_context_id"),
        "context_expires_at_epoch": time.time() + context_ttl_s,
        "last_fused_id": fused_id,
        "last_budget_run_id": budget_run_id,
        "turn_count": int(((prev or {}).get("turn_count") or 0)) + 1,
        "stable_turns": prev_stable + 1 if unchanged else 0,
        "changed_turns": 0 if unchanged else int(((prev or {}).get("changed_turns") or 0)) + 1,
        "updated_at": now_iso(),
    }
    with connect() as con:
        upsert(con, "brainlive_invalidation_state", row, "live_session_id")
        con.commit()


def optimized_hot_brainlive_cycle(
    live_session_id: str,
    *,
    person_id: str | None = None,
    explicit_location: str | None = None,
    gps_json: dict[str, Any] | None = None,
    latest_audio_sample_path: str | None = None,
    latest_speaker_label: str | None = None,
    explicit_speaker_person_id: str | None = None,
    meaningful_signal: bool = True,
    force_context: bool = False,
    identity_ttl_s: float = 180.0,
    context_ttl_s: float = 60.0,
) -> dict[str, Any]:
    """V15.7 loop: same idea as V15.6, but with precise reuse/invalidation.

    It will not recompute identity/place/context if stable. It can still
    rediscover them when context/conversation changes, TTL expires, confidence is
    low, or explicit upstream signals say so.
    """
    ensure_invalidation_schema()
    stages: dict[str, Any] = {}
    t_dec = time.time()
    decision = decide_invalidation(
        live_session_id,
        explicit_location=explicit_location,
        gps_json=gps_json,
        explicit_speaker_person_id=explicit_speaker_person_id,
        latest_speaker_label=latest_speaker_label,
        meaningful_signal=meaningful_signal,
        force_context=force_context,
    )
    stages["invalidation_ms"] = int((time.time() - t_dec) * 1000)

    # Identity: reuse cache unless decision says recompute. resolve_speaker_hot
    # itself also returns cache hits, so this is double-safe.
    t_id = time.time()
    if decision["reuse"].get("identity"):
        speaker = resolve_speaker_hot(live_session_id, explicit_person_id=None, speaker_label=latest_speaker_label, force_verify=False, ttl_s=identity_ttl_s)
    else:
        speaker = resolve_speaker_hot(live_session_id, audio_sample_path=latest_audio_sample_path, explicit_person_id=explicit_speaker_person_id, speaker_label=latest_speaker_label, force_verify=bool(explicit_speaker_person_id or latest_audio_sample_path), ttl_s=identity_ttl_s)
    stages["identity_ms"] = int((time.time() - t_id) * 1000)
    active_people = [speaker["person_id"]] if speaker.get("person_id") else []

    # Place: if stable, use state place; otherwise resolve multi-source.
    t_place = time.time()
    if decision["reuse"].get("place"):
        with connect() as con:
            prev = _latest_state(con, live_session_id)
        place = json_loads((prev or {}).get("place_json"), {}) if prev else {}
        if not place:
            place = resolve_place_multisource(live_session_id, explicit_location=explicit_location, gps_json=gps_json, person_id=person_id)
    else:
        place = resolve_place_multisource(live_session_id, explicit_location=explicit_location, gps_json=gps_json, person_id=person_id)
    stages["place_ms"] = int((time.time() - t_place) * 1000)

    # Context: rebuild only when needed. prepare_hot_context still verifies TTL.
    t_ctx = time.time()
    refresh_level = decision.get("context_refresh")
    if refresh_level == "none":
        hot_ctx = prepare_hot_context(live_session_id, person_id=person_id, active_people=active_people, place=place, reason="reuse_verified", force=False, ttl_s=context_ttl_s)
    elif refresh_level == "vision_only":
        hot_ctx = prepare_hot_context(live_session_id, person_id=person_id, active_people=active_people, place=place, reason="vision_changed_light", force=False, ttl_s=context_ttl_s)
    else:
        hot_ctx = prepare_hot_context(live_session_id, person_id=person_id, active_people=active_people, place=place, reason=f"invalidate_{refresh_level}", force=True, ttl_s=context_ttl_s)
    stages["hot_context_total_ms"] = int((time.time() - t_ctx) * 1000)

    if not meaningful_signal:
        _save_state(live_session_id, decision=decision, speaker=speaker, place=place, hot_context=hot_ctx, fused_id=None, budget_run_id=None, context_ttl_s=context_ttl_s)
        return {"status": "hot_context_ready", "decision": decision, "speaker": speaker, "place": place, "hot_context": {k: v for k, v in hot_ctx.items() if k != "context"}, "stages": stages}

    # Fuse only after the right inputs are hot. build_fused_situation can reuse
    # lower-level rows; avoid forcing context refresh here.
    t_fused = time.time()
    fused = build_fused_situation(live_session_id, person_id=person_id, explicit_location=explicit_location, gps_json=gps_json, force_context_refresh=False, use_llm=True)
    stages["fuse_ms"] = int((time.time() - t_fused) * 1000)

    # Route/predict. If invalidation found no semantic change and no proactive
    # value, router can observe. We still ask router because it is the LLM gate,
    # not a regex rule.
    route = route_triggers_llm(live_session_id, fused=fused, hot_context=hot_ctx, target_ms=12000)
    prediction = run_unified_hot_prediction(live_session_id, fused=fused, hot_context=hot_ctx, route=route, target_ms=12000, timeout=9.0)
    prediction["stages"] = {**stages, **(prediction.get("stages") or {})}
    _save_state(
        live_session_id,
        decision=decision,
        speaker=speaker,
        place=place,
        hot_context=hot_ctx,
        fused_id=fused.get("fused_id"),
        budget_run_id=prediction.get("budget_run_id"),
        context_ttl_s=context_ttl_s,
    )
    return {"status": prediction.get("status"), "decision": decision, "speaker": speaker, "place": place, "hot_context": {k: v for k, v in hot_ctx.items() if k != "context"}, "fused": fused, "route": route, "prediction": prediction, "stages": prediction["stages"]}
