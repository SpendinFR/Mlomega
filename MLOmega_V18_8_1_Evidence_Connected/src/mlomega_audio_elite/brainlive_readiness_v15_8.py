
from __future__ import annotations

"""V15.8 BrainLive readiness audit.

This module verifies the two production questions that matter before running the
live loop:

1) Is there a real inbox for audio / transcripts / images / GPS?
2) Does Brain2 expose the deep context BrainLive needs as hot input?

It does not create a second Brain2 and it does not infer psychology. It only
checks schema/readiness and reports missing deep sources explicitly.
"""

from typing import Any
from .db import connect, init_db
from .utils import json_dumps
from .brainlive_service_v15_5 import brainlive_inbox_status, ensure_service_schema

VERSION = "15.8.0-readiness-audit"


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _count(con, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
    if not _table_exists(con, table):
        return -1
    row = _one(con, f"SELECT COUNT(*) AS n FROM {table} {where}", params) or {"n": 0}
    return int(row["n"])


BRAIN2_LIVE_SOURCES = {
    "self_model": ["self_model_dimensions", "self_model_facts", "v14_self_model_readings", "v14_3_self_model_exports"],
    "memory": ["memory_cards", "source_items", "lifestream_segments", "life_events", "life_event_entities", "memory_facets", "memory_frames"],
    "predictions": ["predictions", "future_scenarios", "trajectory_warnings"],
    "pattern_mirror": ["v14_pattern_mirror_cards", "v14_trajectory_forecasts", "v14_forecast_watch_queue"],
    "people_openloops": ["v14_5_people_context_profiles", "v14_5_personal_open_loops", "v14_5_active_questions", "v14_5_next_best_actions"],
    "interpersonal": ["v14_6_relationship_state_models", "v14_6_interpersonal_loop_cards", "v14_6_social_aftereffects", "v14_6_intervention_suggestions"],
    "proactive_policy": ["v14_7_intervention_policies", "v14_7_intervention_feedback"],
    "clarification": ["v14_8_clarification_items"],
    "identity": ["speaker_profiles", "voice_embeddings"],
    "vision_context": ["vision_frames", "vision_scene_observations"],
    "brainlive_hot_projection": ["brainlive_active_contexts", "brainlive_hot_context_cache", "brainlive_invalidation_state"],
    "brainlive_learning": ["brainlive_short_horizon_forecasts", "brainlive_prediction_outcomes", "brainlive_life_hypotheses", "brainlive_routine_cards", "brainlive_affordance_matches"],
    "personal_live_model": ["brainlive_personal_model_exports", "brainlive_live_relevance_index", "personal_language_patterns", "phrase_templates", "utterance_analyses", "emotion_evidence", "behavior_signals", "confirmed_patterns", "loop_patterns"],
}


def brainlive_brain2_readiness_audit(person_id: str | None = None) -> dict[str, Any]:
    ensure_service_schema()
    init_db()
    with connect() as con:
        if not person_id:
            row = _one(con, "SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1")
            person_id = str(row["person_id"]) if row and row.get("person_id") else "me"
        groups: dict[str, Any] = {}
        missing_tables: list[str] = []
        empty_core: list[str] = []
        for group, tables in BRAIN2_LIVE_SOURCES.items():
            items = []
            group_has_data = False
            for t in tables:
                exists = _table_exists(con, t)
                n = _count(con, t) if exists else -1
                if not exists:
                    missing_tables.append(t)
                if n > 0:
                    group_has_data = True
                items.append({"table": t, "exists": exists, "rows": n})
            groups[group] = {"ready": group_has_data, "tables": items}
        # These are the minimal categories BrainLive needs for useful live magic.
        for core in ["self_model", "memory", "pattern_mirror", "people_openloops", "interpersonal", "proactive_policy", "personal_live_model"]:
            if not groups.get(core, {}).get("ready"):
                empty_core.append(core)
        return {
            "version": VERSION,
            "person_id": person_id,
            "inbox": brainlive_inbox_status(),
            "brain2_live_context": groups,
            "missing_tables": sorted(set(missing_tables)),
            "empty_core_groups": empty_core,
            "verdict": "ready" if not empty_core else "schema_ready_but_needs_brain2_data",
            "meaning": {
                "brain2_does_not_need_to_run_all_day": True,
                "brainlive_reads_hot_context_from_brain2_tables": True,
                "nightly_brain2_should_refresh_these_sources": list(BRAIN2_LIVE_SOURCES.keys()),
            },
        }
