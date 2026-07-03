from __future__ import annotations

"""V15.1 BrainLive longitudinal engines.

This module upgrades BrainLive from a schema/prompt layer into a strict
longitudinal layer:
- routine mining from structured day/life observations;
- LLM-only hypothesis engine for psychological/need/meaning inference;
- automatic outcome evaluation for open short-horizon forecasts;
- LLM-only disagreement interpretation;
- LLM-based personal affordance matching from vision + needs + preferences;
- daily/nightly scheduler plan: BrainLive during the day, Brain2 at night;
- replay bridge that reuses existing Brain2 replay/conversation data when present.

Strict policy: no regex, no keyword psychology, no fixed emotion/meaning lists used
as inference. Deterministic code may aggregate timestamps, source tables and
structured fields. Any cognitive interpretation is delegated to Ollama/Qwen JSON.
"""

from collections import defaultdict
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .brainlive_v15 import build_active_context, ensure_brainlive_schema, run_brainlive, start_live_session, ingest_live_turn
from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id
from .integrity_v176 import (
    ContractValidationError, active_forecast_sql, horizon_due_at, parse_iso_utc,
    quarantine, quarantine_in_transaction, record_forecast_outcome, transition_due_forecasts, validate_outcome_batch,
)

VERSION = "15.1.0-longitudinal-no-keyword-psychology"

LONGITUDINAL_TABLES = {
    "brainlive_routine_mining_runs",
    "brainlive_routine_candidates",
    "brainlive_routine_cards",
    "brainlive_hypothesis_engine_runs",
    "brainlive_hypothesis_comparisons",
    "brainlive_outcome_eval_runs",
    "brainlive_outcome_evaluations",
    "brainlive_disagreement_llm_runs",
    "brainlive_affordance_match_runs",
    "brainlive_scheduler_plans",
    "brainlive_scheduler_events",
    "brainlive_replay_runs",
}

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_routine_mining_runs(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  start_time TEXT,
  end_time TEXT,
  status TEXT NOT NULL,
  source_counts_json TEXT DEFAULT '{}',
  params_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_routine_candidates(
  routine_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  pattern_kind TEXT NOT NULL,
  time_signature_json TEXT DEFAULT '{}',
  location_signature_json TEXT DEFAULT '{}',
  activity_signature_json TEXT DEFAULT '{}',
  people_signature_json TEXT DEFAULT '{}',
  evidence_items_json TEXT DEFAULT '[]',
  support_count INTEGER DEFAULT 0,
  period_count INTEGER DEFAULT 0,
  recurrence_score REAL DEFAULT 0.0,
  stability_score REAL DEFAULT 0.0,
  last_seen_at TEXT,
  llm_interpretation_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'candidate',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_routine_cards(
  routine_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  routine_label TEXT,
  routine_kind TEXT,
  time_signature_json TEXT DEFAULT '{}',
  location_signature_json TEXT DEFAULT '{}',
  activity_signature_json TEXT DEFAULT '{}',
  evidence_items_json TEXT DEFAULT '[]',
  support_count INTEGER DEFAULT 0,
  period_count INTEGER DEFAULT 0,
  recurrence_score REAL DEFAULT 0.0,
  stability_score REAL DEFAULT 0.0,
  interpretation_source TEXT DEFAULT 'statistical_no_llm',
  llm_interpretation_json TEXT DEFAULT '{}',
  confidence REAL DEFAULT 0.0,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_hypothesis_engine_runs(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  source_routine_ids_json TEXT DEFAULT '[]',
  source_hypothesis_ids_json TEXT DEFAULT '[]',
  source_outcome_ids_json TEXT DEFAULT '[]',
  status TEXT NOT NULL,
  qwen_json TEXT DEFAULT '{}',
  created_hypothesis_ids_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_hypothesis_comparisons(
  comparison_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  hypothesis_id TEXT,
  routine_id TEXT,
  comparison_window TEXT,
  support_json TEXT DEFAULT '[]',
  counter_json TEXT DEFAULT '[]',
  next_test_json TEXT DEFAULT '{}',
  confidence_delta REAL DEFAULT 0.0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_outcome_eval_runs(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  status TEXT NOT NULL,
  evaluated_forecasts_json TEXT DEFAULT '[]',
  qwen_json TEXT DEFAULT '{}',
  created_outcome_ids_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_outcome_evaluations(
  evaluation_id TEXT PRIMARY KEY,
  forecast_id TEXT,
  candidate_id TEXT,
  live_session_id TEXT,
  person_id TEXT NOT NULL,
  verdict TEXT DEFAULT 'not_enough_evidence',
  match_score REAL,
  observed_after_json TEXT DEFAULT '{}',
  evaluation_source TEXT DEFAULT 'llm_required_unscored',
  qwen_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_disagreement_llm_runs(
  run_id TEXT PRIMARY KEY,
  disagreement_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  status TEXT NOT NULL,
  qwen_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_affordance_match_runs(
  run_id TEXT PRIMARY KEY,
  live_session_id TEXT,
  person_id TEXT NOT NULL,
  status TEXT NOT NULL,
  qwen_json TEXT DEFAULT '{}',
  matched_affordance_ids_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_scheduler_plans(
  plan_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  timezone TEXT DEFAULT 'Europe/Paris',
  daytime_tick_minutes INTEGER DEFAULT 5,
  brainlive_mode TEXT DEFAULT 'deep_live',
  nightly_time TEXT DEFAULT '03:30',
  brain2_periods_json TEXT DEFAULT '["day"]',
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_scheduler_events(
  event_id TEXT PRIMARY KEY,
  plan_id TEXT,
  person_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_replay_runs(
  replay_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  source TEXT NOT NULL,
  start_time TEXT,
  end_time TEXT,
  status TEXT NOT NULL,
  counts_json TEXT DEFAULT '{}',
  result_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  error_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_bl_routine_person ON brainlive_routine_candidates(person_id, pattern_kind, recurrence_score, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_bl_hyp_engine_person ON brainlive_hypothesis_engine_runs(person_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_out_eval_person ON brainlive_outcome_eval_runs(person_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_sched_person ON brainlive_scheduler_plans(person_id, status);
"""

HYPOTHESIS_SCHEMA = {
    "hypotheses": [
        {
            "hypothesis_type": "routine|preference|need|avoidance|flow|fatigue|relationship|weekly_cycle|emotional_cycle|opportunity|contradiction|other",
            "statement": "",
            "scope": "short|day|week|life",
            "time_pattern": "",
            "location_pattern": "",
            "trigger_contexts": [],
            "candidate_needs": [],
            "candidate_emotions": [],
            "candidate_risks": [],
            "candidate_opportunities": [],
            "predicted_next_window": "",
            "confidence": 0.0,
            "evidence": [],
            "counter_evidence": [],
            "next_tests": [],
        }
    ],
    "comparisons": [
        {"routine_id": "", "support": [], "counter": [], "next_test": {}, "confidence_delta": 0.0}
    ],
    "notes_for_brainlive": [],
}

OUTCOME_SCHEMA = {
    "evaluations": [
        {
            "forecast_id": "",
            "was_prediction_correct": True,
            "match_score": 0.0,
            "observed_after": "",
            "lesson": {},
            "evidence": [],
            "counter_evidence": [],
            "missed_opportunity": {"present": False, "situation_summary": "", "missed_intervention": "", "why_missed": "", "future_rule_learned": "", "confidence": 0.0},
        }
    ]
}

DISAGREEMENT_SCHEMA = {
    "possible_meanings": [
        {"meaning": "", "confidence": 0.0, "evidence": [], "counter_evidence": [], "what_to_watch_next": []}
    ],
    "next_policy": "observe|back_off|ask_later|update_preference|keep_hypothesis_open|other",
    "watch_next": [],
    "hypothesis_updates": [],
}

AFFORDANCE_SCHEMA = {
    "matches": [
        {
            "affordance_label": "",
            "world_element": "",
            "position_hint": "",
            "matched_need_label": "",
            "personal_relevance": "",
            "personal_fit": 0.0,
            "time_sensitivity": 0.0,
            "confidence": 0.0,
            "evidence": [],
            "counter_evidence": [],
            "intervention_candidate": "",
        }
    ],
    "watch_next": [],
}


def _clamp(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return max(0.0, min(1.0, x))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    try:
        row = con.execute(sql, params).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = _one(con, "SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1")
    return str(row["person_id"]) if row and row.get("person_id") else "me"


def _compact(rows: list[dict[str, Any]], limit: int = 80) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        item: dict[str, Any] = {}
        for key, value in row.items():
            if value is None:
                continue
            if isinstance(value, str) and len(value) > 1400:
                item[key] = value[:1400] + "…"
            else:
                item[key] = value
        out.append(item)
    return out


def ensure_longitudinal_schema() -> None:
    init_db()
    ensure_brainlive_schema()
    with connect() as con:
        con.executescript(SCHEMA)
        # brainlive_v15 may have created brainlive_routine_cards first with a smaller schema.
        # Add longitudinal columns idempotently for compatibility.
        existing_cols = {r["name"] for r in con.execute("PRAGMA table_info(brainlive_routine_cards)").fetchall()} if "brainlive_routine_cards" in {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()} else set()
        column_defs = {
            "run_id": "TEXT",
            "pattern_kind": "TEXT",
            "time_signature_json": "TEXT DEFAULT '{}'",
            "evidence_items_json": "TEXT DEFAULT '[]'",
            "period_count": "INTEGER DEFAULT 0",
            "recurrence_score": "REAL DEFAULT 0.0",
            "stability_score": "REAL DEFAULT 0.0",
            "last_seen_at": "TEXT",
            "llm_interpretation_json": "TEXT DEFAULT '{}'",
        }
        for col, ddl in column_defs.items():
            if col not in existing_cols:
                con.execute(f"ALTER TABLE brainlive_routine_cards ADD COLUMN {col} {ddl}")
        con.commit()


def audit_longitudinal() -> dict[str, Any]:
    ensure_longitudinal_schema()
    with connect() as con:
        existing = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for table in sorted(LONGITUDINAL_TABLES):
            if table in existing:
                counts[table] = int(con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
        strict_notes = {
            "no_keyword_psychology": True,
            "no_regex_routing": True,
            "cognitive_inference_requires_llm": True,
            "deterministic_allowed": "timestamp/location/source aggregation only",
        }
    return {"version": VERSION, "missing_tables": sorted(LONGITUDINAL_TABLES - existing), "counts": counts, "strict_policy": strict_notes}


def _event_time(row: dict[str, Any]) -> str | None:
    for key in ("captured_at", "state_time", "timestamp_start", "created_at", "time_start", "started_at"):
        if row.get(key):
            return str(row[key])
    return None


def _collect_structured_day_signals(con, *, person_id: str, start_time: str | None, end_time: str | None, limit: int = 5000) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    time_clause = ""
    params: list[Any] = []
    if start_time:
        time_clause += " AND created_at>=?"
        params.append(start_time)
    if end_time:
        time_clause += " AND created_at<=?"
        params.append(end_time)
    # BrainLive world states: already structured current modes/location/activity.
    for r in _many(con, f"SELECT * FROM brainlive_world_states WHERE person_id=?{time_clause} ORDER BY created_at DESC LIMIT ?", tuple([person_id] + params + [limit])):
        signals.append({
            "source_table": "brainlive_world_states",
            "source_id": r.get("world_state_id"),
            "observed_at": r.get("state_time") or r.get("created_at"),
            "location": r.get("where_am_i"),
            "activity": r.get("active_mode"),
            "people": json_loads(r.get("who_is_active_json"), []),
            "summary": r.get("what_is_happening"),
            "raw": r,
        })
    # Vision observations: structured location/activity/objects/affordances.
    for r in _many(con, f"SELECT o.*, f.captured_at FROM vision_scene_observations o LEFT JOIN vision_frames f ON f.frame_id=o.frame_id WHERE 1=1{time_clause} ORDER BY o.created_at DESC LIMIT ?", tuple(params + [limit])):
        signals.append({
            "source_table": "vision_scene_observations",
            "source_id": r.get("observation_id"),
            "observed_at": r.get("captured_at") or r.get("created_at"),
            "location": r.get("location_hint"),
            "activity": (json_loads(r.get("possible_user_activities_json"), []) or [None])[0],
            "people": [],
            "summary": r.get("scene_summary"),
            "raw": r,
        })
    # Lifestream segments: structured channel/kind/topic-like summaries.
    for r in _many(con, f"SELECT * FROM lifestream_segments WHERE 1=1{time_clause} ORDER BY created_at DESC LIMIT ?", tuple(params + [limit])):
        signals.append({
            "source_table": "lifestream_segments",
            "source_id": r.get("segment_id"),
            "observed_at": r.get("captured_start") or r.get("created_at"),
            "location": None,
            "activity": r.get("segment_kind") or r.get("channel"),
            "people": [r.get("speaker_person_id")] if r.get("speaker_person_id") else [],
            "summary": r.get("observed_summary") or r.get("transcript_text"),
            "raw": r,
        })
    # Memory cards: only structured card type/topic/time, no semantic keyword inference.
    for r in _many(con, f"SELECT * FROM memory_cards WHERE person_id=?{time_clause} ORDER BY updated_at DESC LIMIT ?", tuple([person_id] + params + [limit])):
        signals.append({
            "source_table": "memory_cards",
            "source_id": r.get("card_id"),
            "observed_at": r.get("time_start") or r.get("updated_at") or r.get("created_at"),
            "location": None,
            "activity": r.get("card_type") or r.get("topic"),
            "people": [],
            "summary": r.get("title") or r.get("summary"),
            "raw": r,
        })
    return [s for s in signals if _parse_dt(_event_time(s) or s.get("observed_at"))]


def _time_signature(dt: datetime) -> dict[str, Any]:
    return {
        "weekday": dt.weekday(),
        "weekday_name": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][dt.weekday()],
        "hour_bucket": f"{dt.hour:02d}:00-{dt.hour:02d}:59",
        "date": dt.date().isoformat(),
    }


def _routine_groups(signals: list[dict[str, Any]]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for sig in signals:
        dt = _parse_dt(sig.get("observed_at"))
        if not dt:
            continue
        ts = _time_signature(dt)
        location = sig.get("location") or "unknown_location"
        activity = sig.get("activity") or "unknown_activity"
        groups[("weekly_time_location_activity", ts["weekday"], ts["hour_bucket"], location, activity)].append(sig)
        groups[("weekly_time_activity", ts["weekday"], ts["hour_bucket"], activity)].append(sig)
        groups[("location_activity", location, activity)].append(sig)
    return groups


def mine_routines(*, person_id: str | None = None, start_time: str | None = None, end_time: str | None = None, min_support: int = 3, use_llm: bool = True, timeout: float = 480.0) -> dict[str, Any]:
    """Extract recurrent temporal/location/action candidates from structured days.

    Deterministic part only computes recurrence and stability. It does not infer
    why the user does something. When LLM is enabled, it may add an interpretation
    to the candidate, but the candidate itself remains evidence-first.
    """
    ensure_longitudinal_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        signals = _collect_structured_day_signals(con, person_id=person_id, start_time=start_time, end_time=end_time)
        run_id = stable_id("blroutinerun", person_id, start_time, end_time, now)
        upsert(con, "brainlive_routine_mining_runs", {
            "run_id": run_id,
            "person_id": person_id,
            "start_time": start_time,
            "end_time": end_time,
            "status": "started",
            "source_counts_json": json_dumps({"signals": len(signals)}),
            "params_json": json_dumps({"min_support": min_support, "use_llm": use_llm}),
            "created_at": now,
            "error_text": None,
        }, "run_id")
        groups = _routine_groups(signals)
        candidates: list[dict[str, Any]] = []
        for key, items in groups.items():
            if len(items) < min_support:
                continue
            dates = sorted({(_parse_dt(i.get("observed_at")) or datetime.now(timezone.utc)).date().isoformat() for i in items})
            if len(dates) < min(2, min_support):
                continue
            kind = str(key[0])
            time_sig: dict[str, Any] = {}
            location_sig: dict[str, Any] = {}
            activity_sig: dict[str, Any] = {}
            if kind == "weekly_time_location_activity":
                _, weekday, hour_bucket, location, activity = key
                time_sig = {"weekday": weekday, "hour_bucket": hour_bucket, "dates": dates}
                location_sig = {"location": location}
                activity_sig = {"activity": activity}
            elif kind == "weekly_time_activity":
                _, weekday, hour_bucket, activity = key
                time_sig = {"weekday": weekday, "hour_bucket": hour_bucket, "dates": dates}
                activity_sig = {"activity": activity}
            else:
                _, location, activity = key
                location_sig = {"location": location}
                activity_sig = {"activity": activity}
            last_seen = max((i.get("observed_at") for i in items if i.get("observed_at")), default=now)
            recurrence = min(1.0, len(items) / max(min_support, 8))
            stability = min(1.0, len(dates) / max(2, len(items)))
            routine_id = stable_id("blroutine", person_id, kind, time_sig, location_sig, activity_sig)
            evidence = [{"source_table": i.get("source_table"), "source_id": i.get("source_id"), "observed_at": i.get("observed_at"), "summary": i.get("summary")} for i in items[:40]]
            row = {
                "routine_id": routine_id,
                "person_id": person_id,
                "run_id": run_id,
                "pattern_kind": kind,
                "time_signature_json": json_dumps(time_sig),
                "location_signature_json": json_dumps(location_sig),
                "activity_signature_json": json_dumps(activity_sig),
                "people_signature_json": json_dumps({}),
                "evidence_items_json": json_dumps(evidence),
                "support_count": len(items),
                "period_count": len(dates),
                "recurrence_score": recurrence,
                "stability_score": stability,
                "last_seen_at": last_seen,
                "llm_interpretation_json": json_dumps({}),
                "status": "candidate",
                "created_at": now,
                "updated_at": now,
            }
            upsert(con, "brainlive_routine_candidates", row, "routine_id")
            label = f"Observed recurrence: {kind} support={len(items)}"
            upsert(con, "brainlive_routine_cards", {
                "routine_id": routine_id,
                "person_id": person_id,
                "routine_label": label,
                "routine_kind": kind,
                "temporal_signature_json": json_dumps(time_sig),
                "location_signature_json": json_dumps(location_sig),
                "people_signature_json": json_dumps({}),
                "action_signature_json": json_dumps(activity_sig),
                "trigger_contexts_json": json_dumps([]),
                "inferred_needs_json": json_dumps([]),
                "inferred_emotions_json": json_dumps([]),
                "preferred_affordances_json": json_dumps([]),
                "observed_outcomes_json": json_dumps(evidence),
                "support_count": len(items),
                "counter_count": 0,
                "confidence": recurrence,
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }, "routine_id")
            candidates.append(row)
        con.execute("UPDATE brainlive_routine_mining_runs SET status=?, source_counts_json=? WHERE run_id=?", ("ok", json_dumps({"signals": len(signals), "candidates": len(candidates)}), run_id))
        con.commit()
    if use_llm and candidates:
        # Add LLM interpretation in a second pass; if it fails, candidates stay neutral.
        try:
            run_hypothesis_engine(person_id=person_id, routine_ids=[c["routine_id"] for c in candidates[:80]], timeout=timeout)
        except Exception:
            pass
    return {"run_id": run_id, "person_id": person_id, "signals": len(signals), "candidates": len(candidates), "routine_cards_created_or_updated": len(candidates), "candidate_ids": [c["routine_id"] for c in candidates]}


def run_hypothesis_engine(*, person_id: str | None = None, routine_ids: list[str] | None = None, timeout: float = 480.0) -> dict[str, Any]:
    """Create/test life hypotheses using Qwen only."""
    ensure_longitudinal_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        if routine_ids:
            placeholders = ",".join("?" for _ in routine_ids)
            routines = _many(con, f"SELECT * FROM brainlive_routine_candidates WHERE routine_id IN ({placeholders})", tuple(routine_ids))
        else:
            routines = _many(con, "SELECT * FROM brainlive_routine_candidates WHERE person_id=? ORDER BY recurrence_score DESC, updated_at DESC LIMIT 80", (person_id,))
        existing = _many(con, "SELECT * FROM brainlive_life_hypotheses WHERE person_id=? AND status='active' ORDER BY confidence DESC, updated_at DESC LIMIT 80", (person_id,))
        outcomes = _many(con, "SELECT * FROM brainlive_prediction_outcomes WHERE person_id=? ORDER BY created_at DESC LIMIT 80", (person_id,))
        run_id = stable_id("blhyprun", person_id, now)
        upsert(con, "brainlive_hypothesis_engine_runs", {
            "run_id": run_id,
            "person_id": person_id,
            "source_routine_ids_json": json_dumps([r.get("routine_id") for r in routines]),
            "source_hypothesis_ids_json": json_dumps([h.get("hypothesis_id") for h in existing]),
            "source_outcome_ids_json": json_dumps([o.get("outcome_id") for o in outcomes]),
            "status": "started",
            "qwen_json": json_dumps({}),
            "created_hypothesis_ids_json": json_dumps([]),
            "created_at": now,
            "error_text": None,
        }, "run_id")
        con.commit()
    system = (
        "Tu es le moteur BrainLive V15.1 Hypothesis Engine. Tu transformes des routines/statistiques structurées en hypothèses testables, "
        "avec preuves, contre-preuves, alternatives, prochains tests. Aucun cliché, aucune psychologie générique. Tu ne conclus pas si les preuves sont faibles. JSON strict."
    )
    prompt = json_dumps({
        "mission": "Comparer les routines, hypothèses existantes et outcomes. Créer seulement des hypothèses utiles, concurrentes, testables, avec incertitude.",
        "routines": _compact(routines, 120),
        "existing_hypotheses": _compact(existing, 120),
        "recent_outcomes": _compact(outcomes, 120),
        "rules": ["no hardcoded meanings", "do not confuse routine with need", "include counter-evidence and missing evidence", "create next tests"],
    })
    created: list[str] = []
    qwen_json: dict[str, Any] = {}
    error_text = None
    status = "ok"
    try:
        qwen_json = OllamaJsonClient().require_json(system, prompt, schema_hint=HYPOTHESIS_SCHEMA, timeout=timeout)
    except Exception as exc:
        status = "llm_error"
        error_text = str(exc)[:2000]
    with connect() as con:
        if status == "ok":
            for h in qwen_json.get("hypotheses") or []:
                if not isinstance(h, dict) or not h.get("statement"):
                    continue
                hid = stable_id("blhyp", person_id, h.get("hypothesis_type"), h.get("statement"))
                upsert(con, "brainlive_life_hypotheses", {
                    "hypothesis_id": hid,
                    "person_id": person_id,
                    "hypothesis_type": h.get("hypothesis_type") or "other",
                    "statement": h.get("statement"),
                    "scope": h.get("scope") or "life",
                    "time_pattern": h.get("time_pattern"),
                    "location_pattern": h.get("location_pattern"),
                    "people_pattern_json": json_dumps(h.get("people_pattern") or []),
                    "trigger_contexts_json": json_dumps(h.get("trigger_contexts") or []),
                    "candidate_needs_json": json_dumps(h.get("candidate_needs") or []),
                    "candidate_emotions_json": json_dumps(h.get("candidate_emotions") or []),
                    "candidate_risks_json": json_dumps(h.get("candidate_risks") or []),
                    "candidate_opportunities_json": json_dumps(h.get("candidate_opportunities") or []),
                    "predicted_next_window": h.get("predicted_next_window"),
                    "confidence": _clamp(h.get("confidence")),
                    "evidence_count": len(h.get("evidence") or []),
                    "counter_evidence_count": len(h.get("counter_evidence") or []),
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                }, "hypothesis_id")
                for role, vals in (("support", h.get("evidence") or []), ("counter", h.get("counter_evidence") or [])):
                    for idx, txt in enumerate(vals):
                        eid = stable_id("blhypev", hid, role, idx, txt)
                        upsert(con, "brainlive_hypothesis_evidence", {
                            "evidence_id": eid,
                            "hypothesis_id": hid,
                            "person_id": person_id,
                            "evidence_role": role,
                            "source_table": "brainlive_hypothesis_engine_runs",
                            "source_id": run_id,
                            "evidence_text": str(txt),
                            "weight": 0.6 if role == "support" else -0.4,
                            "observed_at": now,
                            "created_at": now,
                        }, "evidence_id")
                created.append(hid)
            for c in qwen_json.get("comparisons") or []:
                cid = stable_id("blhypcmp", person_id, c.get("routine_id"), c.get("next_test"), now)
                upsert(con, "brainlive_hypothesis_comparisons", {
                    "comparison_id": cid,
                    "person_id": person_id,
                    "hypothesis_id": c.get("hypothesis_id"),
                    "routine_id": c.get("routine_id"),
                    "comparison_window": now[:10],
                    "support_json": json_dumps(c.get("support") or []),
                    "counter_json": json_dumps(c.get("counter") or []),
                    "next_test_json": json_dumps(c.get("next_test") or {}),
                    "confidence_delta": max(-1.0, min(1.0, float(c.get("confidence_delta") or 0.0))),
                    "created_at": now,
                }, "comparison_id")
        con.execute("UPDATE brainlive_hypothesis_engine_runs SET status=?, qwen_json=?, created_hypothesis_ids_json=?, error_text=? WHERE run_id=?", (status, json_dumps(qwen_json), json_dumps(created), error_text, run_id))
        con.commit()
    return {"run_id": run_id, "status": status, "created_hypothesis_ids": created, "error_text": error_text}


def _forecast_due_after(occurred_at: str | None, horizon: str | None) -> str | None:
    """Single H0/H1/H2 contract shared with forecast creation."""
    if not occurred_at:
        return None
    try:
        return horizon_due_at(occurred_at, str(horizon or "H1"))
    except Exception:
        return None


def _observation_event_time(row: dict[str, Any]) -> str | None:
    source = row.get("source_table")
    candidates = {
        "brainlive_turn_buffer": ("timestamp_start", "created_at"),
        "brainlive_world_states": ("state_time", "created_at"),
        "vision_scene_observations": ("captured_at", "created_at"),
    }.get(str(source), ("occurred_at", "created_at"))
    for key in candidates:
        value = row.get(key)
        if not value:
            continue
        try:
            return parse_iso_utc(str(value)).isoformat(timespec="milliseconds")
        except Exception:
            continue
    return None


def _collect_observations_after(con, *, live_session_id: str | None, since: str | None, until: str | None, limit: int = 80) -> list[dict[str, Any]]:
    """Collect later evidence in global event-time order, not table-block order."""
    if not live_session_id or not since:
        return []
    try:
        since_dt = parse_iso_utc(since)
        until_dt = parse_iso_utc(until) if until else None
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    rows += [{"source_table": "brainlive_turn_buffer", **r} for r in _many(con, "SELECT * FROM brainlive_turn_buffer WHERE live_session_id=? ORDER BY created_at LIMIT ?", (live_session_id, limit))]
    rows += [{"source_table": "brainlive_world_states", **r} for r in _many(con, "SELECT * FROM brainlive_world_states WHERE live_session_id=? ORDER BY created_at LIMIT ?", (live_session_id, limit))]
    rows += [{"source_table": "vision_scene_observations", **r} for r in _many(con, "SELECT o.*,f.captured_at FROM vision_scene_observations o LEFT JOIN vision_frames f ON f.frame_id=o.frame_id WHERE o.live_session_id=? ORDER BY o.created_at LIMIT ?", (live_session_id, limit))]
    ordered: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        event_time = _observation_event_time(row)
        if not event_time:
            continue
        try:
            dt = parse_iso_utc(event_time)
        except Exception:
            continue
        if dt < since_dt or (until_dt is not None and dt > until_dt):
            continue
        item = dict(row)
        item["event_time"] = event_time
        ordered.append((dt, item))
    ordered.sort(key=lambda pair: pair[0])
    return _compact([item for _, item in ordered[:limit]], limit)


def evaluate_outcomes_auto(*, person_id: str | None = None, limit: int = 30, timeout: float = 480.0) -> dict[str, Any]:
    """Evaluate only due forecasts, with strict ownership and terminal lifecycle."""
    ensure_longitudinal_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        transition_due_forecasts(con, as_of=now, person_id=person_id)
        forecasts = _many(con, f"""
            SELECT f.* FROM brainlive_short_horizon_forecasts f
            WHERE f.person_id=? AND {active_forecast_sql('f')}
              AND COALESCE(f.lifecycle_state, f.status)='due'
              AND f.canonical_outcome_id IS NULL
            ORDER BY COALESCE(f.due_at, f.created_at) ASC LIMIT ?
        """, (person_id, limit))
        due: list[dict[str, Any]] = []
        for f in forecasts:
            occurred_at = f.get("occurred_at") or f.get("created_at")
            due_after = f.get("due_at") or _forecast_due_after(occurred_at, f.get("horizon"))
            if not occurred_at or not due_after:
                continue
            observations = _collect_observations_after(
                con, live_session_id=f.get("live_session_id"), since=occurred_at, until=due_after
            )
            due.append({"forecast": f, "due_after": due_after, "observations_after": observations})
        run_id = stable_id("bloutcomeeval", person_id, now)
        upsert(con, "brainlive_outcome_eval_runs", {
            "run_id": run_id,
            "person_id": person_id,
            "status": "started",
            "evaluated_forecasts_json": json_dumps([d["forecast"].get("forecast_id") for d in due]),
            "qwen_json": json_dumps({}),
            "created_outcome_ids_json": json_dumps([]),
            "created_at": now,
            "error_text": None,
        }, "run_id")
        con.commit()
    if not due:
        with connect() as con:
            con.execute("UPDATE brainlive_outcome_eval_runs SET status=? WHERE run_id=?", ("ok_no_due_forecasts", run_id))
            con.commit()
        return {"run_id": run_id, "status": "ok_no_due_forecasts", "evaluated": 0}

    system = "Tu es BrainLive V15.1 Outcome Evaluator. Tu compares une prédiction court-terme à des observations datées postérieures. Tu ne produis aucune évaluation hors de la liste fournie. JSON strict."
    prompt = json_dumps({"mission": "Évaluer les prévisions dues avec les observations postérieures ordonnées. Toute absence de preuve doit laisser la prévision indeterminate, jamais correcte par défaut.", "due_forecasts": due})
    raw_output: dict[str, Any] | None = None
    qwen_json: dict[str, Any] = {}
    status = "ok"
    error_text = None
    try:
        raw_output = OllamaJsonClient().require_json(system, prompt, schema_hint=OUTCOME_SCHEMA, timeout=timeout)
        qwen_json = validate_outcome_batch(raw_output)
    except ContractValidationError as exc:
        status = "quarantined_invalid_llm_output"
        error_text = str(exc)[:2000]
    except Exception as exc:
        status = "llm_error"
        error_text = str(exc)[:2000]

    created: list[str] = []
    allowed_ids = {str(d["forecast"].get("forecast_id")) for d in due}
    with connect() as con:
        if status == "ok":
            for ev in qwen_json.get("evaluations") or []:
                forecast_id = str(ev.get("forecast_id") or "")
                if forecast_id not in allowed_ids:
                    # The model must not evaluate another forecast merely because
                    # it knows/guesses an ID.
                    quarantine_in_transaction(con, category="outcome_out_of_scope", reason="LLM returned a forecast outside the due set", raw_payload=ev, run_id=run_id, source_table="brainlive_outcome_eval_runs", source_id=run_id, person_id=person_id)
                    continue
                forecast = _one(con, "SELECT * FROM brainlive_short_horizon_forecasts WHERE forecast_id=? AND person_id=?", (forecast_id, person_id))
                if not forecast:
                    continue
                try:
                    result = record_forecast_outcome(
                        con,
                        forecast_id=forecast_id,
                        person_id=person_id,
                        observed_after=ev.get("observed_after"),
                        was_prediction_correct=ev.get("was_prediction_correct"),
                        match_score=ev.get("match_score"),
                        outcome_window="auto",
                        actor="outcome_evaluator",
                        lesson=ev.get("lesson") or {},
                        evidence=ev.get("evidence") or [],
                    )
                except Exception as exc:
                    quarantine_in_transaction(con, category="outcome_write_rejected", reason=str(exc)[:2000], raw_payload=ev, run_id=run_id, source_table="brainlive_outcome_eval_runs", source_id=forecast_id, person_id=person_id)
                    continue
                created.append(str(result["outcome_id"]))
                mo = ev.get("missed_opportunity") if isinstance(ev.get("missed_opportunity"), dict) else {}
                if mo.get("present"):
                    mid = stable_id("blmissed", person_id, forecast_id, mo.get("situation_summary"), run_id)
                    upsert(con, "brainlive_missed_opportunity_cards", {
                        "missed_id": mid,
                        "live_session_id": forecast.get("live_session_id"),
                        "person_id": person_id,
                        "situation_summary": mo.get("situation_summary") or "",
                        "missed_intervention": mo.get("missed_intervention"),
                        "why_missed": mo.get("why_missed"),
                        "future_rule_learned": mo.get("future_rule_learned"),
                        "evidence_json": json_dumps(ev.get("evidence") or []),
                        "confidence": mo.get("confidence", 0.0),
                        "status": "open",
                        "created_at": now,
                        "updated_at": now,
                    }, "missed_id")
        con.execute(
            "UPDATE brainlive_outcome_eval_runs SET status=?, qwen_json=?, created_outcome_ids_json=?, error_text=? WHERE run_id=?",
            (status, json_dumps(qwen_json), json_dumps(created), error_text, run_id),
        )
        con.commit()
    if status == "quarantined_invalid_llm_output":
        quarantine(category="invalid_outcome_llm_contract", reason=error_text or "invalid outcome payload", raw_payload=raw_output, run_id=run_id, source_table="brainlive_outcome_eval_runs", source_id=run_id, person_id=person_id)
    return {"run_id": run_id, "status": status, "evaluated": len(created), "created_outcome_ids": created, "error_text": error_text}


def interpret_disagreement_llm(disagreement_id: str, *, timeout: float = 480.0) -> dict[str, Any]:
    ensure_longitudinal_schema()
    now = now_iso()
    with connect() as con:
        d = _one(con, "SELECT * FROM brainlive_user_disagreement_events WHERE disagreement_id=?", (disagreement_id,))
        if not d:
            raise ValueError(f"Disagreement introuvable: {disagreement_id}")
        person_id = d["person_id"]
        candidate = _one(con, "SELECT * FROM brainlive_intervention_candidates WHERE candidate_id=?", (d.get("candidate_id"),)) if d.get("candidate_id") else None
        recent = _collect_observations_after(con, live_session_id=d.get("live_session_id"), since=d.get("created_at"), until=None, limit=40)
        hyps = _many(con, "SELECT * FROM brainlive_life_hypotheses WHERE person_id=? AND status='active' ORDER BY confidence DESC LIMIT 60", (person_id,))
        run_id = stable_id("bldisllm", disagreement_id, now)
        upsert(con, "brainlive_disagreement_llm_runs", {"run_id": run_id, "disagreement_id": disagreement_id, "person_id": person_id, "status": "started", "qwen_json": json_dumps({}), "created_at": now, "error_text": None}, "run_id")
        con.commit()
    system = "Tu es BrainLive V15.1 User Disagreement Interpreter. Tu interprètes un désaccord utilisateur sans ego, sans insister, sans supposer. Tu distingues erreur système, mauvais timing, autonomie, flow, évitement possible, fatigue possible, et tu proposes quoi observer ensuite. JSON strict."
    prompt = json_dumps({"disagreement": d, "candidate_intervention": candidate, "observations_after": recent, "active_hypotheses": _compact(hyps, 80)})
    status = "ok"
    error_text = None
    qwen_json: dict[str, Any] = {}
    try:
        qwen_json = OllamaJsonClient().require_json(system, prompt, schema_hint=DISAGREEMENT_SCHEMA, timeout=timeout)
    except Exception as exc:
        status = "llm_error"
        error_text = str(exc)[:2000]
    with connect() as con:
        if status == "ok":
            con.execute("UPDATE brainlive_user_disagreement_events SET possible_meanings_json=?, next_policy=?, watch_next_json=?, updated_at=? WHERE disagreement_id=?", (json_dumps(qwen_json.get("possible_meanings") or []), qwen_json.get("next_policy") or "observe", json_dumps(qwen_json.get("watch_next") or []), now, disagreement_id))
        con.execute("UPDATE brainlive_disagreement_llm_runs SET status=?, qwen_json=?, error_text=? WHERE run_id=?", (status, json_dumps(qwen_json), error_text, run_id))
        con.commit()
    return {"run_id": run_id, "status": status, "disagreement_id": disagreement_id, "output": qwen_json, "error_text": error_text}


def match_personal_affordances(*, live_session_id: str, timeout: float = 480.0) -> dict[str, Any]:
    """Compare recent vision with live needs and learned preferences using LLM."""
    ensure_longitudinal_schema()
    now = now_iso()
    ctx_result = build_active_context(live_session_id, limit=40)
    context = ctx_result["context"]
    person_id = context["session"]["person_id"]
    with connect() as con:
        needs = _many(con, "SELECT * FROM brainlive_need_predictions WHERE live_session_id=? AND status='active' ORDER BY confidence DESC, created_at DESC LIMIT 50", (live_session_id,))
        routines = _many(con, "SELECT * FROM brainlive_routine_candidates WHERE person_id=? ORDER BY recurrence_score DESC, updated_at DESC LIMIT 50", (person_id,))
        hyps = _many(con, "SELECT * FROM brainlive_life_hypotheses WHERE person_id=? AND status='active' ORDER BY confidence DESC LIMIT 50", (person_id,))
        observations = _many(con, "SELECT * FROM vision_scene_observations WHERE live_session_id=? ORDER BY created_at DESC LIMIT 20", (live_session_id,))
        run_id = stable_id("blaffordmatch", live_session_id, now)
        upsert(con, "brainlive_affordance_match_runs", {"run_id": run_id, "live_session_id": live_session_id, "person_id": person_id, "status": "started", "qwen_json": json_dumps({}), "matched_affordance_ids_json": json_dumps([]), "created_at": now, "error_text": None}, "run_id")
        con.commit()
    system = "Tu es BrainLive V15.1 Personal Affordance Matcher. Tu compares ce que le monde visible offre avec les besoins probables et préférences apprises de l'utilisateur. Tu ne décris pas seulement l'image: tu dis ce qui est utile maintenant, avec preuves et contre-preuves. JSON strict."
    prompt = json_dumps({"active_context": context, "recent_needs": _compact(needs, 60), "routine_candidates": _compact(routines, 60), "life_hypotheses": _compact(hyps, 60), "vision_observations": _compact(observations, 40), "rules": ["no hardcoded activity labels", "personal fit requires evidence", "do not intervene if obvious or intrusive"]})
    status = "ok"
    error_text = None
    qwen_json: dict[str, Any] = {}
    ids: list[str] = []
    try:
        qwen_json = OllamaJsonClient().require_json(system, prompt, schema_hint=AFFORDANCE_SCHEMA, timeout=timeout)
    except Exception as exc:
        status = "llm_error"
        error_text = str(exc)[:2000]
    with connect() as con:
        if status == "ok":
            for m in qwen_json.get("matches") or []:
                if not isinstance(m, dict) or not m.get("affordance_label"):
                    continue
                aid = stable_id("blafford", live_session_id, m.get("affordance_label"), m.get("world_element"), now)
                upsert(con, "brainlive_affordances", {
                    "affordance_id": aid,
                    "live_session_id": live_session_id,
                    "frame_id": None,
                    "event_id": None,
                    "person_id": person_id,
                    "affordance_label": m.get("affordance_label"),
                    "world_element": m.get("world_element"),
                    "position_hint": m.get("position_hint"),
                    "personal_relevance": m.get("personal_relevance"),
                    "matched_need_id": m.get("matched_need_label"),
                    "personal_fit": _clamp(m.get("personal_fit")),
                    "time_sensitivity": _clamp(m.get("time_sensitivity")),
                    "evidence_json": json_dumps(m.get("evidence") or []),
                    "counter_evidence_json": json_dumps(m.get("counter_evidence") or []),
                    "confidence": _clamp(m.get("confidence")),
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                }, "affordance_id")
                ids.append(aid)
                if m.get("intervention_candidate"):
                    cid = stable_id("blcand", live_session_id, m.get("intervention_candidate"), aid, now)
                    upsert(con, "brainlive_intervention_candidates", {
                        "candidate_id": cid,
                        "live_session_id": live_session_id,
                        "event_id": None,
                        "run_id": run_id,
                        "person_id": person_id,
                        "message": m.get("intervention_candidate"),
                        "intervention_type": "affordance_hint",
                        "recommended_timing": "now" if _clamp(m.get("time_sensitivity")) >= 0.65 else "soon",
                        "urgency": _clamp(m.get("time_sensitivity")),
                        "confidence": _clamp(m.get("confidence")),
                        "expected_gain": _clamp(m.get("personal_fit")),
                        "risk_if_silent": _clamp(m.get("time_sensitivity")),
                        "risk_if_said": 0.2,
                        "intrusion_score": 0.25,
                        "autonomy_risk": 0.2,
                        "evidence_json": json_dumps(m.get("evidence") or []),
                        "counter_evidence_json": json_dumps(m.get("counter_evidence") or []),
                        "status": "candidate",
                        "gate_decision": "speak_now" if _clamp(m.get("personal_fit")) >= 0.60 and _clamp(m.get("confidence")) >= 0.55 else "queue_or_wait",
                        "gate_reason": "personal_affordance_match",
                        "cooldown_key": aid,
                        "created_at": now,
                        "updated_at": now,
                    }, "candidate_id")
        con.execute("UPDATE brainlive_affordance_match_runs SET status=?, qwen_json=?, matched_affordance_ids_json=?, error_text=? WHERE run_id=?", (status, json_dumps(qwen_json), json_dumps(ids), error_text, run_id))
        con.commit()
    return {"run_id": run_id, "status": status, "matched_affordance_ids": ids, "error_text": error_text}


def configure_daily_nightly_scheduler(*, person_id: str | None = None, timezone_name: str = "Europe/Paris", daytime_tick_minutes: int = 5, nightly_time: str = "03:30", brain2_periods: list[str] | None = None) -> dict[str, Any]:
    ensure_longitudinal_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        plan_id = stable_id("blsched", person_id)
        upsert(con, "brainlive_scheduler_plans", {
            "plan_id": plan_id,
            "person_id": person_id,
            "timezone": timezone_name,
            "daytime_tick_minutes": int(daytime_tick_minutes),
            "brainlive_mode": "deep_live",
            "nightly_time": nightly_time,
            "brain2_periods_json": json_dumps(brain2_periods or ["day", "week", "month"]),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }, "plan_id")
        con.commit()
    return {"plan_id": plan_id, "person_id": person_id, "mode": "BrainLive day / Brain2 night", "nightly_time": nightly_time}


def scheduler_tick(*, person_id: str | None = None, kind: str = "daytime", live_session_id: str | None = None, run_date: str | None = None, timeout: float = 480.0) -> dict[str, Any]:
    ensure_longitudinal_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        plan = _one(con, "SELECT * FROM brainlive_scheduler_plans WHERE person_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1", (person_id,))
        if not plan:
            plan = configure_daily_nightly_scheduler(person_id=person_id)
            plan = _one(con, "SELECT * FROM brainlive_scheduler_plans WHERE plan_id=?", (plan["plan_id"],))
    details: dict[str, Any] = {}
    status = "ok"
    error_text = None
    try:
        if kind == "daytime":
            if live_session_id:
                details["brainlive"] = run_brainlive(live_session_id, mode="deep_live", timeout=timeout)
                details["affordance_match"] = match_personal_affordances(live_session_id=live_session_id, timeout=timeout)
            details["outcome_eval"] = evaluate_outcomes_auto(person_id=person_id, timeout=timeout)
        elif kind == "nightly":
            details["routines"] = mine_routines(person_id=person_id, end_time=now, use_llm=False)
            details["hypotheses"] = run_hypothesis_engine(person_id=person_id, timeout=timeout)
            details["outcomes"] = evaluate_outcomes_auto(person_id=person_id, timeout=timeout)
            from .brainlive_v15 import run_nightly_bridge
            details["brain2_bridge"] = run_nightly_bridge(person_id=person_id, run_date=run_date, force=True)
            try:
                periods = json_loads(plan.get("brain2_periods_json"), ["day", "week", "month"]) if isinstance(plan, dict) else ["day", "week", "month"]
                from .brain2_longitudinal_cases_v17 import run_longitudinal_consolidation
                details["v17_longitudinal"] = []
                for period in periods:
                    if period in {"day", "week", "month", "quarter", "year", "all_time"}:
                        details["v17_longitudinal"].append(run_longitudinal_consolidation(person_id=person_id, period=period, run_date=run_date, use_llm=True, run_periodic_mirror_layer=(period in {"week", "month"})))
            except Exception as exc:
                details["v17_longitudinal"] = {"status": "error", "error": str(exc)[:2000]}
            # V15.9: after Brain2 has consolidated, compile the live-ready
            # personal operating model BrainLive will preload the next day.
            try:
                from .brain2_life_model_v15_10 import build_brain2_canonical_life_model
                details["brain2_canonical_life_model"] = build_brain2_canonical_life_model(person_id=person_id, use_llm=True, timeout=min(timeout, 240.0), limit=120)
            except Exception as exc:
                details["brain2_canonical_life_model"] = {"status": "error", "error": str(exc)[:2000]}
            try:
                from .brainlive_personal_model_v15_9 import build_brain2_live_personal_model
                details["brain2_live_personal_model"] = build_brain2_live_personal_model(person_id=person_id, use_llm=True, timeout=min(timeout, 180.0), limit=80)
            except Exception as exc:
                details["brain2_live_personal_model"] = {"status": "error", "error": str(exc)[:2000]}
        else:
            raise ValueError("kind must be daytime or nightly")
    except Exception as exc:
        status = "error"
        error_text = str(exc)[:2000]
    with connect() as con:
        eid = stable_id("blschedevent", person_id, kind, now)
        upsert(con, "brainlive_scheduler_events", {"event_id": eid, "plan_id": plan.get("plan_id") if isinstance(plan, dict) else None, "person_id": person_id, "event_type": kind, "status": status, "details_json": json_dumps(details), "created_at": now, "error_text": error_text}, "event_id")
        con.commit()
    return {"status": status, "kind": kind, "details": details, "error_text": error_text}


def replay_offline(*, person_id: str | None = None, conversation_id: str | None = None, start_time: str | None = None, end_time: str | None = None, step_turns: int = 8, timeout: float = 480.0) -> dict[str, Any]:
    """Replay existing Brain2/conversation data through BrainLive.

    If Brain2 replay events exist, they are noted as the preferred source. When a
    conversation_id is provided, this function replays turns in chunks and runs
    BrainLive after each chunk. This is not a separate Brain2 simulator; it is a
    bridge that lets BrainLive use existing data to ask: would I have anticipated
    something useful at that point?
    """
    ensure_longitudinal_schema()
    now = now_iso()
    results: list[dict[str, Any]] = []
    source = "conversation_turns"
    error_text = None
    status = "ok"
    with connect() as con:
        person_id = person_id or _default_user(con)
        brain2_replays = _many(con, "SELECT * FROM v13_replay_events ORDER BY created_at DESC LIMIT 20")
        if brain2_replays and not conversation_id:
            source = "brain2_v13_replay_events_available"
        if conversation_id:
            turns = _many(con, "SELECT * FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,))
        else:
            turns = []
    try:
        if turns:
            sess = start_live_session(person_id=person_id, title=f"offline replay {conversation_id}", mode="replay")
            sid = sess["live_session_id"]
            for i, turn in enumerate(turns, start=1):
                ingest_live_turn(sid, turn.get("text") or "", speaker_label=turn.get("speaker_label"), speaker_person_id=turn.get("person_id"), is_final=True)
                if i % max(1, step_turns) == 0 or i == len(turns):
                    results.append(run_brainlive(sid, mode="offline_replay", timeout=timeout, limit=50))
        elif brain2_replays:
            results.append({"source": "v13_replay_events", "available": len(brain2_replays), "note": "Brain2 replay exists; pass --conversation-id to run turn-level BrainLive replay."})
        else:
            status = "no_replay_source"
    except Exception as exc:
        status = "error"
        error_text = str(exc)[:2000]
    with connect() as con:
        rid = stable_id("blreplay", person_id, conversation_id, start_time, end_time, now)
        upsert(con, "brainlive_replay_runs", {"replay_id": rid, "person_id": person_id, "source": source, "start_time": start_time, "end_time": end_time, "status": status, "counts_json": json_dumps({"runs": len(results)}), "result_json": json_dumps(_compact(results, 20)), "created_at": now, "error_text": error_text}, "replay_id")
        con.commit()
    return {"replay_id": rid, "status": status, "source": source, "runs": len(results), "error_text": error_text}


def evaluate_prediction_outcomes(*, person_id: str | None = None, live_session_id: str | None = None, use_llm: bool = True, timeout: float = 480.0, limit: int = 30) -> dict[str, Any]:
    """Compatibility and strict evaluator entrypoint.

    If use_llm=False, forecasts are not scored. The system records an explicit
    llm_required_unscored evaluation instead of pretending to know whether the
    forecast was correct.
    """
    ensure_longitudinal_schema()
    now = now_iso()
    if use_llm:
        return evaluate_outcomes_auto(person_id=person_id, limit=limit, timeout=timeout)
    # Lack of an evaluator is not evidence.  Do not write a permanent
    # ``not_enough_evidence`` row that prevents the real evaluator later.
    with connect() as con:
        person_id = person_id or _default_user(con)
        params: list[Any] = [person_id]
        sql = f"SELECT forecast_id FROM brainlive_short_horizon_forecasts f WHERE f.person_id=? AND {active_forecast_sql('f')}"
        if live_session_id:
            sql += " AND f.live_session_id=?"
            params.append(live_session_id)
        sql += " ORDER BY COALESCE(f.due_at, f.created_at) ASC LIMIT ?"
        params.append(limit)
        pending = [r.get("forecast_id") for r in _many(con, sql, tuple(params))]
    return {"person_id": person_id, "evaluated": 0, "status": "llm_required_no_mutation", "pending_forecast_ids": pending}

# V18 remediation: historical replay is isolated and event-time bounded.  It
# never creates a production BrainLive session or rewrites historical clocks.
from .v18_replay import install as _install_v18_replay
_globals_v18_replay = _install_v18_replay(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_replay)
