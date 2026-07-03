"""V19 visual-context adapter.

Publishes the current WorldBrain state and scene observations into the REAL
core tables owned by ``brainlive_v15`` — ``brainlive_world_states`` and
``vision_scene_observations`` — so the ``v18_context`` wrapper picks them up
automatically (it reads ``raw["context"]["world_state"]`` and
``raw["context"]["visual_context"]``).

Critical constraints (audit fix, priority 1):
- This module NEVER creates ``brainlive_world_states`` or
  ``vision_scene_observations``. Those tables belong to ``brainlive_v15`` and
  already exist in production with a different schema than an earlier draft of
  this module assumed. We call the core ``ensure_brainlive_schema()`` instead.
- Inserts use the REAL columns (verified against ``brainlive_v15.py``):
    * ``brainlive_world_states``: world_state_id, live_session_id (FK NOT NULL),
      person_id, state_time, where_am_i, who_is_active_json, what_is_happening,
      probable_activity_json, active_emotional_state, active_mode,
      audio_context_json, visual_context_json, evidence_json,
      counter_evidence_json, confidence, created_at.
    * ``vision_scene_observations``: observation_id, frame_id (FK NOT NULL),
      live_session_id, conversation_id, model, scene_summary, location_hint,
      people_count, spatial_context, social_context_hint, visible_text_json,
      objects_json, risks_json, affordances_json, possible_user_activities_json,
      personal_relevance_json, confidence, raw_json, created_at.
- FK integrity is enforced (``PRAGMA foreign_keys=ON``): ``brainlive_world_states``
  requires a ``brainlive_sessions`` row and ``vision_scene_observations`` requires
  a ``vision_frames`` row. This module ensures those parents exist before writing.
"""
from __future__ import annotations

import os
from typing import Any

from .db import connect, init_db, insert_only, write_transaction
from .utils import json_dumps, now_iso, stable_id
from .v19_self_schema import ensure_self_schema, get_self_schema


def _ensure_core_visual_schema(db_path=None) -> None:
    """Ensure the REAL core tables exist by delegating to brainlive_v15.

    ``ensure_brainlive_schema`` ignores an explicit ``db_path`` argument and
    resolves the database from ``MLOMEGA_DB``; mirror the ``v19_keyframes``
    pattern so tests that pass ``db_path`` still target the right file.
    """
    init_db(db_path)
    ensure_self_schema(db_path)
    from .brainlive_v15 import ensure_brainlive_schema

    old = os.environ.get("MLOMEGA_DB")
    if db_path is not None:
        os.environ["MLOMEGA_DB"] = str(db_path)
    try:
        ensure_brainlive_schema()
    finally:
        if db_path is not None:
            if old is not None:
                os.environ["MLOMEGA_DB"] = old
            else:
                os.environ.pop("MLOMEGA_DB", None)


# Backwards-compatible name kept for callers/tests. It no longer creates the
# core tables itself — it delegates to the brainlive_v15 ensure.
def ensure_visual_context_schema(db_path=None) -> None:
    _ensure_core_visual_schema(db_path)


def _ensure_session(con, *, person_id: str, live_session_id: str, now: str) -> None:
    """Guarantee a parent ``brainlive_sessions`` row for the FK."""
    insert_only(
        con,
        "brainlive_sessions",
        {
            "live_session_id": live_session_id,
            "person_id": person_id,
            "started_at": now,
            "ended_at": None,
            "status": "active",
            "session_title": None,
            "active_location_hint": None,
            "active_people_json": json_dumps([]),
            "active_conversation_id": None,
            "current_mode": "unknown",
            "h0_goal": None,
            "h1_goal": None,
            "h2_goal": None,
            "metadata_json": json_dumps({"created_by": "v19_visual_context"}),
            "created_at": now,
            "updated_at": now,
        },
        on_conflict="ignore",
    )


def _ensure_frame(con, *, frame_id: str, live_session_id: str, obs: dict[str, Any], now: str) -> None:
    """Guarantee a parent ``vision_frames`` row for the FK (insert-only/immutable)."""
    insert_only(
        con,
        "vision_frames",
        {
            "frame_id": frame_id,
            "source_asset_id": obs.get("source_asset_id"),
            "conversation_id": obs.get("conversation_id"),
            "live_session_id": live_session_id,
            "captured_at": obs.get("captured_at") or obs.get("created_at") or now,
            "image_path": obs.get("image_path"),
            "image_sha256": obs.get("image_sha256") or obs.get("sha256"),
            "width": obs.get("width"),
            "height": obs.get("height"),
            "device_source": obs.get("device_source") or "xr",
            "capture_mode": obs.get("capture_mode") or "xr_keyframe",
            "metadata_json": json_dumps({"created_by": "v19_visual_context"}),
            "created_at": now,
        },
        on_conflict="ignore",
    )


def publish_visual_context(
    *,
    person_id: str,
    live_session_id: str,
    world_state: dict[str, Any] | None = None,
    observations: list[dict[str, Any]] | None = None,
    db_path=None,
) -> dict[str, Any]:
    """Publish WorldBrain state + scene observations into the REAL core tables.

    Returns the ids written plus the compact ``self_schema_hot`` / ``scene_focus``
    projections used by the hot capsule extension (E19).
    """
    _ensure_core_visual_schema(db_path)
    now = now_iso()
    ids: list[str] = []
    with connect(db_path) as con, write_transaction(con):
        _ensure_session(con, person_id=person_id, live_session_id=live_session_id, now=now)

        if world_state is not None:
            wid = str(world_state.get("world_state_id") or stable_id("worldstate", person_id, live_session_id, now))
            insert_only(
                con,
                "brainlive_world_states",
                {
                    "world_state_id": wid,
                    "live_session_id": live_session_id,
                    "person_id": person_id,
                    "state_time": world_state.get("state_time") or now,
                    "where_am_i": world_state.get("where_am_i") or world_state.get("place"),
                    "who_is_active_json": json_dumps(world_state.get("who_is_active") or []),
                    "what_is_happening": world_state.get("what_is_happening") or world_state.get("focus"),
                    "probable_activity_json": json_dumps(world_state.get("probable_activity") or []),
                    "active_emotional_state": world_state.get("active_emotional_state"),
                    "active_mode": world_state.get("active_mode"),
                    "audio_context_json": json_dumps(world_state.get("audio_context") or {}),
                    "visual_context_json": json_dumps(world_state.get("visual_context") or world_state),
                    "evidence_json": json_dumps(world_state.get("evidence") or []),
                    "counter_evidence_json": json_dumps(world_state.get("counter_evidence") or []),
                    "confidence": float(world_state.get("confidence") if world_state.get("confidence") is not None else 0.8),
                    "created_at": now,
                },
                on_conflict="ignore",
            )
            ids.append(wid)

        for obs in observations or []:
            frame_id = str(obs.get("frame_id") or stable_id("v19frame", person_id, live_session_id, obs.get("image_sha256") or obs.get("sha256") or now, obs.get("model") or ""))
            _ensure_frame(con, frame_id=frame_id, live_session_id=live_session_id, obs=obs, now=now)
            oid = str(obs.get("observation_id") or stable_id("obs", person_id, live_session_id, frame_id, obs.get("model") or "", now))
            insert_only(
                con,
                "vision_scene_observations",
                {
                    "observation_id": oid,
                    "frame_id": frame_id,
                    "live_session_id": live_session_id,
                    "conversation_id": obs.get("conversation_id"),
                    "model": str(obs.get("model") or "v19_visual_context"),
                    "scene_summary": obs.get("scene_summary") or obs.get("summary") or obs.get("scene"),
                    "location_hint": obs.get("location_hint") or obs.get("place"),
                    "people_count": obs.get("people_count"),
                    "spatial_context": obs.get("spatial_context"),
                    "social_context_hint": obs.get("social_context_hint"),
                    "visible_text_json": json_dumps(obs.get("visible_text") or []),
                    "objects_json": json_dumps(obs.get("objects") or obs.get("visible_objects") or []),
                    "risks_json": json_dumps(obs.get("risks") or []),
                    "affordances_json": json_dumps(obs.get("affordances") or []),
                    "possible_user_activities_json": json_dumps(obs.get("possible_user_activities") or []),
                    "personal_relevance_json": json_dumps(obs.get("personal_relevance") or {}),
                    "confidence": float(obs.get("confidence") if obs.get("confidence") is not None else 0.8),
                    "raw_json": json_dumps(obs),
                    "created_at": obs.get("created_at") or now,
                },
                on_conflict="ignore",
            )
            ids.append(oid)

    return {
        "status": "completed",
        "ids": ids,
        "self_schema_hot": get_self_schema(person_id=person_id, db_path=db_path, limit=5),
        "scene_focus": (world_state or {}).get("focus") or (world_state or {}).get("what_is_happening"),
    }
