from __future__ import annotations

"""V15.12 BrainLive <-> Brain2 coordination layer.

This module does **not** create a second Brain2 inside BrainLive. It coordinates
what each side already does:

- Brain2 already creates short/medium/long predictions, hypotheses, forecasts,
  trajectory warnings, self-model, relationship models and V15.10 Life Model.
- BrainLive observes the present, activates relevant Brain2 watch hooks, predicts
  H0/H1/H2, speaks or stays silent, then stores what happened.

V15.12 makes the two brains communicate cleanly:

1. Brain2 forecasts/hypotheses -> live watch bindings used by BrainLive.
2. BrainLive day package -> clean evidence packet for Brain2 consolidation.
3. Live prediction/intervention/outcome -> Brain2 reconciliation verdicts.
4. Canonical model lifecycle -> recent/old/stale/contradicted/active status.
5. Exact live context snapshots -> audit/debug of why BrainLive acted or stayed
   silent.

Strict policy: no regex/keyword psychology. Deterministic code only moves,
counts, timestamps and links existing evidence. Interpretation/reconciliation is
LLM JSON when enabled; otherwise records are stored as raw/llm_required.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v18_legacy_forecasts import active_legacy_forecasts as _active_v14_forecasts

VERSION = "15.12.0-brainlive-brain2-coordination"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_day_packages(
  package_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  status TEXT NOT NULL,
  live_sessions_json TEXT DEFAULT '[]',
  turns_json TEXT DEFAULT '[]',
  sensor_events_json TEXT DEFAULT '[]',
  context_snapshots_json TEXT DEFAULT '[]',
  predictions_json TEXT DEFAULT '[]',
  interventions_json TEXT DEFAULT '[]',
  silences_json TEXT DEFAULT '[]',
  outcomes_json TEXT DEFAULT '[]',
  vision_json TEXT DEFAULT '[]',
  event_bundles_json TEXT DEFAULT '[]',
  disagreements_json TEXT DEFAULT '[]',
  source_counts_json TEXT DEFAULT '{}',
  llm_summary_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brain2_live_watch_bindings(
  binding_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  hook_name TEXT NOT NULL,
  horizon TEXT NOT NULL DEFAULT 'H1',
  domain TEXT,
  active_person_hint TEXT,
  risk_type TEXT,
  user_common_bad_move TEXT,
  recommended_micro_move TEXT,
  do_not_say_json TEXT DEFAULT '[]',
  intervention_mode TEXT DEFAULT 'watch',
  outcome_success_count INTEGER DEFAULT 0,
  outcome_failure_count INTEGER DEFAULT 0,
  calibration_score REAL DEFAULT 0.0,
  use_policy TEXT DEFAULT 'silent_context',
  activation_conditions_json TEXT DEFAULT '[]',
  predicts_json TEXT DEFAULT '{}',
  watch_signals_json TEXT DEFAULT '[]',
  proactive_options_json TEXT DEFAULT '[]',
  silence_policy_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  llm_required INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_brain2_reconciliations(
  reconciliation_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_id TEXT,
  live_source_table TEXT NOT NULL,
  live_source_id TEXT NOT NULL,
  brain2_source_table TEXT,
  brain2_source_id TEXT,
  verdict TEXT NOT NULL DEFAULT 'unscored',
  verdict_confidence REAL DEFAULT 0.0,
  what_brainlive_thought_json TEXT DEFAULT '{}',
  what_happened_json TEXT DEFAULT '{}',
  what_brain2_knows_json TEXT DEFAULT '{}',
  learning_delta_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brain2_life_model_lifecycle(
  lifecycle_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  first_seen_at TEXT,
  last_seen_at TEXT,
  last_confirmed_at TEXT,
  last_contradicted_at TEXT,
  recency_weight REAL DEFAULT 1.0,
  staleness_score REAL DEFAULT 0.0,
  confidence_delta REAL DEFAULT 0.0,
  validity_status TEXT DEFAULT 'active',
  obsolete_reason TEXT,
  evidence_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_context_snapshots_v1512(
  snapshot_id TEXT PRIMARY KEY,
  live_session_id TEXT,
  person_id TEXT NOT NULL,
  source_table TEXT,
  source_id TEXT,
  snapshot_kind TEXT NOT NULL,
  active_people_json TEXT DEFAULT '[]',
  place_json TEXT DEFAULT '{}',
  topic_json TEXT DEFAULT '{}',
  brain2_life_model_json TEXT DEFAULT '{}',
  watch_bindings_json TEXT DEFAULT '[]',
  brain2_forecasts_json TEXT DEFAULT '[]',
  brainlive_state_json TEXT DEFAULT '{}',
  digest TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brain2_brainlive_coordination_runs(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  run_kind TEXT NOT NULL,
  package_id TEXT,
  counts_json TEXT DEFAULT '{}',
  status TEXT NOT NULL,
  error_text TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_bl_day_pkg_person_date ON brainlive_day_packages(person_id, package_date, created_at);
CREATE INDEX IF NOT EXISTS idx_b2_watch_bindings_person ON brain2_live_watch_bindings(person_id, status, confidence);
CREATE INDEX IF NOT EXISTS idx_bl_b2_rec_person ON brainlive_brain2_reconciliations(person_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_b2_lifecycle_source ON brain2_life_model_lifecycle(person_id, source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_bl_ctx_snap_session ON brainlive_context_snapshots_v1512(live_session_id, created_at);
"""

WATCH_BINDING_SCHEMA: dict[str, Any] = {
    "watch_bindings": [
        {
            "source_table": "predictions|future_scenarios|trajectory_warnings|v14_trajectory_forecasts|v14_forecast_watch_queue|brain2_live_prediction_hooks|brain2_personal_routine_models|brain2_need_expectation_models|brain2_emotional_trajectory_models|brain2_live_affordance_preferences",
            "source_id": "",
            "hook_name": "",
            "horizon": "H0|H1|H2|day|week|long",
            "domain": "negotiation|conflict|client|relationship|routine|project|regulation|unknown",
            "active_person_hint": "optional",
            "risk_type": "avoidance|escalation|delay|misread|overcommit|unknown",
            "user_common_bad_move": "",
            "recommended_micro_move": "",
            "do_not_say": [],
            "intervention_mode": "watch|silent_context|queue|speak_now|avoid_intervention",
            "use_policy": "watch_only|silent_context|proactive_allowed|strong_live_hook|do_not_use",
            "activation_conditions": [],
            "predicts": {},
            "watch_signals": [],
            "proactive_options": [],
            "silence_policy": {},
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "missing_for_live_activation": [],
}

RECONCILIATION_SCHEMA: dict[str, Any] = {
    "reconciliations": [
        {
            "live_source_table": "brainlive_short_horizon_forecasts|brainlive_hot_budget_runs|brainlive_intervention_candidates|brainlive_hot_intervention_log|brainlive_prediction_outcomes|brainlive_missed_opportunity_cards",
            "live_source_id": "",
            "brain2_source_table": "predictions|future_scenarios|trajectory_warnings|v14_trajectory_forecasts|v14_forecast_watch_queue|brain2_live_prediction_hooks|brain2_personal_routine_models|unknown",
            "brain2_source_id": "",
            "verdict": "confirmed|contradicted|partially_confirmed|too_early|too_late|wrong_context|useful_silence|missed_opportunity|unscored",
            "verdict_confidence": 0.0,
            "what_brainlive_thought": {},
            "what_happened": {},
            "what_brain2_knows": {},
            "learning_delta": {},
        }
    ],
    "summary_for_brain2": [],
    "summary_for_brainlive": [],
}

DAY_PACKAGE_SCHEMA: dict[str, Any] = {
    "day_summary": "",
    "important_live_moments": [],
    "prediction_lessons": [],
    "intervention_lessons": [],
    "silence_lessons": [],
    "model_update_candidates": [],
    "questions_for_brain2": [],
}

CANONICAL_TABLES = [
    ("brain2_personal_routine_models", "routine_id"),
    ("brain2_place_preference_models", "place_model_id"),
    ("brain2_action_preference_models", "action_model_id"),
    ("brain2_need_expectation_models", "need_model_id"),
    ("brain2_expression_state_models", "expression_model_id"),
    ("brain2_emotional_trajectory_models", "trajectory_model_id"),
    ("brain2_contextual_self_models", "contextual_model_id"),
    ("brain2_live_prediction_hooks", "hook_id"),
    ("brain2_live_affordance_preferences", "affordance_pref_id"),
]


def ensure_coordination_schema() -> None:
    init_db()
    # Coordination reads Brain2 Life Model tables such as
    # brain2_live_prediction_hooks.  Do not rely on a pre-existing shipped DB to
    # provide them; create the upstream schema explicitly for clean installs.
    try:
        from .brain2_life_model_v15_10 import ensure_life_model_schema
        ensure_life_model_schema()
    except Exception:
        # The coordination schema itself should still be installable; query sites
        # already tolerate missing optional source tables.
        pass
    try:
        from .brain2_life_model_updater_v15_13 import ensure_life_model_updater_schema
        ensure_life_model_updater_schema()
    except Exception:
        # V15.13 lifecycle is strongly preferred, but clean installs should not
        # fail just because the updater module cannot be imported in a partial env.
        pass
    with connect() as con:
        con.executescript(SCHEMA)
        # V15.14 adds complete BrainLive event bundles; keep old DBs migratable.
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(brainlive_day_packages)").fetchall()}
            if "event_bundles_json" not in cols:
                con.execute("ALTER TABLE brainlive_day_packages ADD COLUMN event_bundles_json TEXT DEFAULT '[]'")
        except Exception:
            pass
        # V16.2: tactical/domain columns on watch bindings so BrainLive can route
        # negotiation/conflict/client/routine hooks directly instead of parsing
        # generic JSON blobs.
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(brain2_live_watch_bindings)").fetchall()}
            for name, ddl in {
                "domain": "TEXT",
                "active_person_hint": "TEXT",
                "risk_type": "TEXT",
                "user_common_bad_move": "TEXT",
                "recommended_micro_move": "TEXT",
                "do_not_say_json": "TEXT DEFAULT '[]'",
                "intervention_mode": "TEXT DEFAULT 'watch'",
                "outcome_success_count": "INTEGER DEFAULT 0",
                "outcome_failure_count": "INTEGER DEFAULT 0",
                "calibration_score": "REAL DEFAULT 0.0",
                "use_policy": "TEXT DEFAULT 'silent_context'",
            }.items():
                if name not in cols:
                    con.execute(f"ALTER TABLE brain2_live_watch_bindings ADD COLUMN {name} {ddl}")
        except Exception:
            pass
        con.commit()


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _columns(con, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _query(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
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


def _compact(rows: list[dict[str, Any]], limit: int = 80, max_str: int = 1200) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        nr: dict[str, Any] = {}
        for k, v in r.items():
            if v is None:
                continue
            if isinstance(v, str) and len(v) > max_str:
                nr[k] = v[:max_str] + "…"
            else:
                nr[k] = v
        out.append(nr)
    return out


def _count(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for k, v in payload.items():
        if isinstance(v, list):
            counts[k] = len(v)
        elif isinstance(v, dict):
            counts[k] = sum(len(x) if isinstance(x, list) else 1 for x in v.values())
        else:
            counts[k] = 1 if v else 0
    return counts


def _local_tz():
    tz_name = os.environ.get("MLOMEGA_LOCAL_TZ", "Europe/Paris")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _iso_date(s: str | None = None) -> str:
    if not s:
        return datetime.now(_local_tz()).date().isoformat()
    return s[:10]


def _date_bounds(package_date: str | None) -> tuple[str, str, str]:
    tz = _local_tz()
    d = datetime.fromisoformat((package_date or _iso_date())[:10]).replace(tzinfo=tz)
    start = d.astimezone(timezone.utc).isoformat()
    end = (d + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    return d.date().isoformat(), start, end


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _row_in_period(row: dict[str, Any], start: str, end: str, *keys: str) -> bool:
    sdt = _parse_dt(start); edt = _parse_dt(end)
    if sdt is None or edt is None:
        return True
    for k in keys:
        if row.get(k):
            dt = _parse_dt(row.get(k))
            if dt is not None:
                return sdt <= dt < edt
    return False


def _filter_period(rows: list[dict[str, Any]], start: str, end: str, *keys: str) -> list[dict[str, Any]]:
    return [r for r in rows if _row_in_period(r, start, end, *keys)]


def _clamp(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except Exception:
        v = default
    return max(0.0, min(1.0, v))


def _canonical_life_rows_for_live(con, table: str, pk: str, person_id: str, limit: int = 120) -> list[dict[str, Any]]:
    if not _table_exists(con, table):
        return []
    cols = _columns(con, table)
    lifecycle_block = ""
    params: tuple[Any, ...]
    if _table_exists(con, "brain2_life_model_item_lifecycle"):
        lifecycle_block = f"""
          AND NOT EXISTS (
            SELECT 1 FROM brain2_life_model_item_lifecycle lc
            WHERE lc.person_id=? AND lc.source_table=? AND lc.source_id=t.{pk}
              AND (COALESCE(lc.truth_status,'candidate') IN ('contradicted','obsolete','rejected','false','wrong')
                   OR COALESCE(lc.use_policy,'watch_only') IN ('do_not_use','forbidden','never_use'))
          )
        """
        params = (person_id, person_id, table, limit)
    else:
        params = (person_id, limit)
    use_policy_clause = ""
    if "use_policy" in cols:
        use_policy_clause = "AND COALESCE(t.use_policy,'silent_context') NOT IN ('do_not_use','forbidden','never_use')"
    order_time = "t.updated_at" if "updated_at" in cols else ("t.created_at" if "created_at" in cols else pk)
    confidence_expr = "COALESCE(t.confidence,0.5)" if "confidence" in cols else "0.5"
    return _query(con, f"""
        SELECT t.* FROM {table} t
        WHERE t.person_id=? AND COALESCE(t.status,'active')='active'
          {use_policy_clause}
          {lifecycle_block}
        ORDER BY {confidence_expr} DESC, {order_time} DESC
        LIMIT ?
    """, params)


def _active_live_prediction_hooks(con, person_id: str, limit: int = 120) -> list[dict[str, Any]]:
    """Return only Brain2 hooks that BrainLive is allowed to see.

    V15.13 lifecycle is the authority for revocation. A hook can still have
    status='active' in the canonical table for history, but contradicted,
    obsolete, rejected, forbidden or do_not_use lifecycle rows must block it
    before it reaches live context.
    """
    if not _table_exists(con, "brain2_live_prediction_hooks"):
        return []
    if _table_exists(con, "brain2_life_model_item_lifecycle"):
        return _query(con, """
            SELECT h.*,
                   lc.truth_status AS lifecycle_truth_status,
                   lc.use_policy AS lifecycle_use_policy,
                   lc.stratum AS lifecycle_stratum,
                   lc.confidence AS lifecycle_confidence
            FROM brain2_live_prediction_hooks h
            LEFT JOIN brain2_life_model_item_lifecycle lc
              ON lc.lifecycle_id = (
                SELECT lc2.lifecycle_id
                FROM brain2_life_model_item_lifecycle lc2
                WHERE lc2.person_id=h.person_id
                  AND lc2.source_table='brain2_live_prediction_hooks'
                  AND lc2.source_id=h.hook_id
                  AND lc2.truth_status NOT IN ('contradicted','obsolete','rejected')
                  AND COALESCE(lc2.use_policy, 'silent_context') NOT IN ('do_not_use','forbidden')
                ORDER BY lc2.updated_at DESC
                LIMIT 1
              )
            WHERE h.person_id=?
              AND h.status='active'
              AND COALESCE(h.use_policy, 'silent_context') NOT IN ('do_not_use','forbidden')
              AND NOT EXISTS (
                SELECT 1
                FROM brain2_life_model_item_lifecycle bad
                WHERE bad.person_id=h.person_id
                  AND bad.source_table='brain2_live_prediction_hooks'
                  AND bad.source_id=h.hook_id
                  AND (bad.truth_status IN ('contradicted','obsolete','rejected')
                       OR COALESCE(bad.use_policy, '') IN ('do_not_use','forbidden'))
              )
            ORDER BY h.confidence DESC, COALESCE(lc.updated_at, h.updated_at) DESC
            LIMIT ?
        """, (person_id, limit))
    return _query(con, """
        SELECT * FROM brain2_live_prediction_hooks
        WHERE person_id=? AND status='active'
          AND COALESCE(use_policy, 'silent_context') NOT IN ('do_not_use','forbidden')
        ORDER BY confidence DESC, updated_at DESC LIMIT ?
    """, (person_id, limit))


def _run_llm(system: str, payload: dict[str, Any], schema: dict[str, Any], *, timeout: float) -> tuple[dict[str, Any], str | None]:
    try:
        out = OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=timeout)
        return out, None
    except Exception as exc:
        return {"llm_required": True, "error": str(exc)[:1200]}, str(exc)[:1200]


def collect_day_evidence(person_id: str, *, package_date: str | None = None, limit: int = 200) -> dict[str, Any]:
    ensure_coordination_schema()
    day, start, end = _date_bounds(package_date)
    with connect() as con:
        sessions = _query(con, """
            SELECT * FROM brainlive_sessions
            WHERE person_id=?
              AND started_at<?
              AND (ended_at IS NULL OR ended_at>=?)
            ORDER BY started_at
        """, (person_id, end, start)) if _table_exists(con, "brainlive_sessions") else []
        session_ids = [s.get("live_session_id") for s in sessions if s.get("live_session_id")]
        placeholders = ",".join("?" for _ in session_ids) or "''"
        params = tuple(session_ids)
        by_session = f"live_session_id IN ({placeholders})" if session_ids else "1=0"

        def _session_rows(table: str, order_col: str = "created_at", max_rows: int | None = None) -> list[dict[str, Any]]:
            if not _table_exists(con, table):
                return []
            rows = _query(con, f"SELECT * FROM {table} WHERE {by_session} ORDER BY {order_col}", params)
            rows = _filter_period(rows, start, end, "timestamp_start", "timestamp_end", "created_at", "loaded_at", "updated_at", "window_start", "state_time", "captured_at")
            return _compact(rows, max_rows or limit)

        payload: dict[str, Any] = {
            "package_date": day,
            "period_start": start,
            "period_end": end,
            "live_sessions": _compact(sessions, limit),
            "turns": _session_rows("brainlive_turn_buffer", "COALESCE(timestamp_start, created_at)", limit),
            "sensor_events": _session_rows("brainlive_sensor_events", "created_at", limit),
            "context_snapshots": _session_rows("brainlive_active_contexts", "loaded_at DESC", 50),
            "hot_contexts": _session_rows("brainlive_hot_context_cache", "updated_at DESC", 50),
            "hot_budget_runs": _session_rows("brainlive_hot_budget_runs", "created_at", limit),
            "short_horizon_forecasts": _compact(_query(con, "SELECT * FROM brainlive_short_horizon_forecasts WHERE person_id=? AND created_at>=? AND created_at<? ORDER BY created_at", (person_id, start, end)), limit) if _table_exists(con, "brainlive_short_horizon_forecasts") else [],
            "intervention_candidates": _compact(_query(con, "SELECT * FROM brainlive_intervention_candidates WHERE person_id=? AND created_at>=? AND created_at<? ORDER BY created_at", (person_id, start, end)), limit) if _table_exists(con, "brainlive_intervention_candidates") else [],
            "hot_interventions": _session_rows("brainlive_hot_intervention_log", "created_at", limit),
            "prediction_outcomes": _compact(_query(con, "SELECT * FROM brainlive_prediction_outcomes WHERE person_id=? AND created_at>=? AND created_at<? ORDER BY created_at", (person_id, start, end)), limit) if _table_exists(con, "brainlive_prediction_outcomes") else [],
            "outcome_evaluations": _compact(_query(con, "SELECT * FROM brainlive_outcome_evaluations WHERE person_id=? AND created_at>=? AND created_at<? ORDER BY created_at", (person_id, start, end)), limit) if _table_exists(con, "brainlive_outcome_evaluations") else [],
            "disagreements": _compact(_query(con, "SELECT * FROM brainlive_user_disagreement_events WHERE person_id=? AND created_at>=? AND created_at<? ORDER BY created_at", (person_id, start, end)), limit) if _table_exists(con, "brainlive_user_disagreement_events") else [],
            "missed_opportunities": _compact(_query(con, "SELECT * FROM brainlive_missed_opportunity_cards WHERE person_id=? AND created_at>=? AND created_at<? ORDER BY created_at", (person_id, start, end)), limit) if _table_exists(con, "brainlive_missed_opportunity_cards") else [],
            "affordance_matches": _compact(_query(con, "SELECT * FROM brainlive_affordance_matches WHERE person_id=? AND created_at>=? AND created_at<? ORDER BY created_at", (person_id, start, end)), limit) if _table_exists(con, "brainlive_affordance_matches") else [],
            "vision_observations": _session_rows("vision_scene_observations", "created_at", limit),
            # V15.14: complete offline event bundles. These are not live summaries;
            # they preserve full multimodal evidence assembled after the day so
            # Brain2 can analyze complete scenes with its existing V13/V14 engines.
            "event_bundles": _compact(_query(con, "SELECT * FROM brainlive_event_bundles_v1514 WHERE person_id=? AND package_date=? AND COALESCE(status,'assembled')!='superseded' ORDER BY start_time", (person_id, day)), limit) if _table_exists(con, "brainlive_event_bundles_v1514") else [],
            "brain2_event_exports": _compact(_query(con, """
                SELECT e.*
                FROM brainlive_brain2_event_exports_v1514 e
                JOIN brainlive_event_bundles_v1514 b ON b.bundle_id=e.bundle_id
                WHERE e.person_id=? AND b.package_date=?
                  AND COALESCE(e.export_status,'exported')!='superseded'
                  AND COALESCE(b.status,'assembled')!='superseded'
                ORDER BY e.created_at DESC
            """, (person_id, day)), limit) if _table_exists(con, "brainlive_brain2_event_exports_v1514") and _table_exists(con, "brainlive_event_bundles_v1514") else [],
        }
        return payload


def create_brainlive_day_package(person_id: str = "me", *, package_date: str | None = None, use_llm: bool = True, timeout: float = 120.0, limit: int = 200) -> dict[str, Any]:
    ensure_coordination_schema()
    raw = collect_day_evidence(person_id, package_date=package_date, limit=limit)
    day = raw["package_date"]; start = raw["period_start"]; end = raw["period_end"]
    llm_summary: dict[str, Any]
    error: str | None = None
    if use_llm:
        llm_summary, error = _run_llm(
            "Tu es le coordinateur BrainLive→Brain2. Résume uniquement les traces live observées; ne crée pas de psychologie sans preuves. Réponds en JSON strict.",
            {"mission": "Prépare un paquet journalier pour Brain2: moments live importants, prédictions H0/H1/H2, interventions, silences, outcomes, incertitudes et éléments à consolider.", "brainlive_day_evidence": raw, "schema": DAY_PACKAGE_SCHEMA},
            DAY_PACKAGE_SCHEMA,
            timeout=timeout,
        )
        status = "llm_ready" if not error else "raw_ready_llm_required"
    else:
        llm_summary = {"llm_required": True, "raw_evidence_available": True}
        status = "raw_only_llm_disabled"
    now = now_iso()
    package_id = stable_id("bldaypkg", person_id, day, now)
    counts = _count(raw)
    with connect() as con:
        upsert(con, "brainlive_day_packages", {
            "package_id": package_id, "person_id": person_id, "package_date": day, "period_start": start, "period_end": end, "status": status,
            "live_sessions_json": json_dumps(raw.get("live_sessions") or []),
            "turns_json": json_dumps(raw.get("turns") or []),
            "sensor_events_json": json_dumps(raw.get("sensor_events") or []),
            "context_snapshots_json": json_dumps((raw.get("context_snapshots") or []) + (raw.get("hot_contexts") or [])),
            "predictions_json": json_dumps((raw.get("short_horizon_forecasts") or []) + (raw.get("hot_budget_runs") or [])),
            "interventions_json": json_dumps((raw.get("intervention_candidates") or []) + (raw.get("hot_interventions") or [])),
            "silences_json": json_dumps([]),
            "outcomes_json": json_dumps((raw.get("prediction_outcomes") or []) + (raw.get("outcome_evaluations") or [])),
            "vision_json": json_dumps((raw.get("vision_observations") or []) + (raw.get("affordance_matches") or [])),
            "event_bundles_json": json_dumps((raw.get("event_bundles") or []) + (raw.get("brain2_event_exports") or [])),
            "disagreements_json": json_dumps((raw.get("disagreements") or []) + (raw.get("missed_opportunities") or [])),
            "source_counts_json": json_dumps(counts), "llm_summary_json": json_dumps(llm_summary), "created_at": now, "updated_at": now,
        }, "package_id")
        con.commit()
    return {"version": VERSION, "package_id": package_id, "person_id": person_id, "package_date": day, "status": status, "source_counts": counts, "llm_summary": llm_summary}


def collect_brain2_forecast_evidence(person_id: str, *, limit: int = 120) -> dict[str, Any]:
    ensure_coordination_schema()
    with connect() as con:
        return {
            "predictions_short_and_next": _compact(_query(con, "SELECT * FROM predictions WHERE (person_id=? OR person_id IS NULL) AND status IN ('open','active','watch') ORDER BY CASE WHEN horizon IN ('next','H0','H1','H2','short','short_term') THEN 0 ELSE 1 END, confidence DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
            "prediction_cases": _compact(_query(con, "SELECT * FROM prediction_cases WHERE (person_id=? OR person_id IS NULL) AND usable_for_prediction=1 ORDER BY quality_score DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
            "future_scenarios": _compact(_query(con, "SELECT * FROM future_scenarios WHERE (person_id=? OR person_id IS NULL) AND status IN ('open','active') ORDER BY probability DESC, risk_level DESC, opportunity_level DESC LIMIT ?", (person_id, limit)), limit),
            "trajectory_warnings": _compact(_query(con, "SELECT * FROM trajectory_warnings WHERE (person_id=? OR person_id IS NULL) AND status IN ('open','active') ORDER BY severity DESC, probability DESC LIMIT ?", (person_id, limit)), limit),
            "v14_trajectory_forecasts": _compact(_active_v14_forecasts(con, person_id, "v14_trajectory_forecasts", limit), limit),
            "v14_forecast_watch_queue": _compact(_active_v14_forecasts(con, person_id, "v14_forecast_watch_queue", limit), limit),
            "brain2_live_prediction_hooks": _compact(_active_live_prediction_hooks(con, person_id, limit), limit),
            "life_model_routines": _compact(_canonical_life_rows_for_live(con, "brain2_personal_routine_models", "routine_id", person_id, limit), limit),
            "life_model_needs": _compact(_canonical_life_rows_for_live(con, "brain2_need_expectation_models", "need_model_id", person_id, limit), limit),
            "life_model_trajectories": _compact(_canonical_life_rows_for_live(con, "brain2_emotional_trajectory_models", "trajectory_model_id", person_id, limit), limit),
            "life_model_affordances": _compact(_canonical_life_rows_for_live(con, "brain2_live_affordance_preferences", "affordance_pref_id", person_id, limit), limit),
        }



SOURCE_SECTION_TABLE_MAP = {
    "predictions_short_and_next": "predictions",
    "prediction_cases": "prediction_cases",
    "future_scenarios": "future_scenarios",
    "trajectory_warnings": "trajectory_warnings",
    "v14_trajectory_forecasts": "v14_trajectory_forecasts",
    "v14_forecast_watch_queue": "v14_forecast_watch_queue",
    "brain2_live_prediction_hooks": "brain2_live_prediction_hooks",
    "life_model_routines": "brain2_personal_routine_models",
    "life_model_needs": "brain2_need_expectation_models",
    "life_model_trajectories": "brain2_emotional_trajectory_models",
    "life_model_affordances": "brain2_live_affordance_preferences",
}

SOURCE_PK_MAP = {
    "predictions": "prediction_id",
    "prediction_cases": "case_id",
    "future_scenarios": "scenario_id",
    "trajectory_warnings": "warning_id",
    "v14_trajectory_forecasts": "forecast_id",
    "v14_forecast_watch_queue": "watch_id",
    "brain2_live_prediction_hooks": "hook_id",
    "brain2_personal_routine_models": "routine_id",
    "brain2_need_expectation_models": "need_model_id",
    "brain2_emotional_trajectory_models": "trajectory_model_id",
    "brain2_live_affordance_preferences": "affordance_pref_id",
}


def _normalize_source_table(name: Any) -> str:
    raw = str(name or "unknown")
    return SOURCE_SECTION_TABLE_MAP.get(raw, raw)


def _valid_source_ref(con, person_id: str, source_table: str, source_id: str) -> tuple[bool, str | None]:
    if source_table not in SOURCE_PK_MAP:
        return False, "unsupported_source_table"
    if not _table_exists(con, source_table):
        return False, "missing_source_table"
    pk = SOURCE_PK_MAP[source_table]
    row = _one(con, f"SELECT * FROM {source_table} WHERE {pk}=?", (source_id,))
    if not row:
        return False, "missing_source_id"
    if row.get("person_id") not in (None, "", person_id):
        return False, "wrong_person_id"
    st = str(row.get("status") or "active").lower()
    if st.startswith("closed") or st in {"contradicted", "obsolete", "rejected", "inactive", "disabled", "disabled_verified_wrong", "superseded"}:
        return False, f"inactive_status:{st}"
    if source_table == "prediction_cases" and int(row.get("usable_for_prediction") or 0) != 1:
        return False, "prediction_case_not_usable"
    if str(row.get("use_policy") or "").lower() in {"do_not_use", "forbidden", "never_use"}:
        return False, "forbidden_use_policy"
    if _table_exists(con, "brain2_life_model_item_lifecycle"):
        bad = _one(con, """
            SELECT * FROM brain2_life_model_item_lifecycle
            WHERE person_id=? AND source_table=? AND source_id=?
              AND (COALESCE(truth_status,'candidate') IN ('contradicted','obsolete','rejected','false','wrong')
                   OR COALESCE(use_policy,'watch_only') IN ('do_not_use','forbidden','never_use'))
            ORDER BY updated_at DESC LIMIT 1
        """, (person_id, source_table, source_id))
        if bad:
            return False, "blocked_by_lifecycle"
    return True, None

def compile_brain2_forecasts_to_live_bindings(person_id: str = "me", *, use_llm: bool = True, timeout: float = 120.0, limit: int = 120) -> dict[str, Any]:
    """Turn existing Brain2 predictions/hypotheses into live watch bindings.

    This is the exact separation we want: Brain2 predicts and hypothesizes;
    BrainLive does not redo that. BrainLive receives watch bindings that say what
    to watch in the present and what H0/H1/H2 action may be useful.
    """
    ensure_coordination_schema()
    evidence = collect_brain2_forecast_evidence(person_id, limit=limit)
    now = now_iso()
    if use_llm:
        out, error = _run_llm(
            "Tu es le compilateur Brain2→BrainLive. Transforme les prédictions/hypothèses Brain2 existantes en hooks live surveillables. Ne crée pas de nouveau modèle de vie; ne fais que rendre actionnable ce qui existe déjà.",
            {"mission": "Convertis les prédictions next/short/mid/long, forecasts V14, Life Model et watch queues en live watch bindings H0/H1/H2/day/week/long.", "brain2_forecast_evidence": evidence, "schema": WATCH_BINDING_SCHEMA},
            WATCH_BINDING_SCHEMA,
            timeout=timeout,
        )
        items = out.get("watch_bindings") if not error and isinstance(out, dict) else []
        llm_required = 0 if items else 1
        status = "llm_ready" if items else "raw_ready_llm_required"
    else:
        out = {"llm_required": True, "raw_evidence_available": True}
        error = None
        items = []
        llm_required = 1
        status = "raw_only_llm_disabled"
        # Raw bindings preserve source references without interpretation.
        for section, rows in evidence.items():
            for r in rows[:20]:
                sid = r.get("prediction_id") or r.get("scenario_id") or r.get("warning_id") or r.get("forecast_id") or r.get("watch_id") or r.get("hook_id") or r.get("routine_id") or r.get("need_model_id") or r.get("trajectory_model_id") or r.get("affordance_pref_id")
                if sid:
                    source_table = _normalize_source_table(section)
                    items.append({"source_table": source_table, "source_id": sid, "hook_name": f"raw:{source_table}:{sid}", "horizon": r.get("horizon") or "H1", "activation_conditions": [], "predicts": {}, "watch_signals": [], "proactive_options": [], "silence_policy": {}, "evidence": [sid], "counter_evidence": [], "confidence": r.get("confidence") or r.get("probability") or 0.5})
    created: list[str] = []
    with connect() as con:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            source_table = _normalize_source_table(item.get("source_table"))[:200]
            source_id = str(item.get("source_id") or item.get("hook_name") or "unknown")[:500]
            hook_name = str(item.get("hook_name") or f"{source_table}:{source_id}")[:500]
            is_valid, invalid_reason = _valid_source_ref(con, person_id, source_table, source_id)
            binding_id = stable_id("b2livebind", person_id, source_table, source_id, hook_name)
            binding_status = "active" if is_valid else "unresolved_source"
            upsert(con, "brain2_live_watch_bindings", {
                "binding_id": binding_id, "person_id": person_id, "source_table": source_table, "source_id": source_id, "hook_name": hook_name,
                "horizon": item.get("horizon") or "H1",
                "domain": item.get("domain"),
                "active_person_hint": item.get("active_person_hint") or item.get("person_hint"),
                "risk_type": item.get("risk_type"),
                "user_common_bad_move": item.get("user_common_bad_move"),
                "recommended_micro_move": item.get("recommended_micro_move"),
                "do_not_say_json": json_dumps(item.get("do_not_say") or []),
                "intervention_mode": item.get("intervention_mode") or "watch",
                "outcome_success_count": int(item.get("outcome_success_count") or 0),
                "outcome_failure_count": int(item.get("outcome_failure_count") or 0),
                "calibration_score": _clamp(item.get("calibration_score"), 0.0),
                "use_policy": item.get("use_policy") or "silent_context",
                "activation_conditions_json": json_dumps(item.get("activation_conditions") or []),
                "predicts_json": json_dumps(item.get("predicts") or {}), "watch_signals_json": json_dumps(item.get("watch_signals") or []),
                "proactive_options_json": json_dumps(item.get("proactive_options") or []), "silence_policy_json": json_dumps(item.get("silence_policy") or {}),
                "evidence_json": json_dumps(item.get("evidence") or []), "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence"), 0.5), "status": binding_status, "llm_required": llm_required,
                "created_at": now, "updated_at": now,
            }, "binding_id")
            if is_valid:
                created.append(binding_id)
        con.commit()
    return {"version": VERSION, "person_id": person_id, "status": status, "bindings_created": len(created), "binding_ids": created, "source_counts": _count(evidence), "llm_output": out}


def reconcile_brainlive_with_brain2(person_id: str = "me", *, package_id: str | None = None, use_llm: bool = True, timeout: float = 180.0, limit: int = 100) -> dict[str, Any]:
    ensure_coordination_schema()
    with connect() as con:
        pkg = None
        if package_id:
            pkg = _one(con, "SELECT * FROM brainlive_day_packages WHERE package_id=?", (package_id,))
        if not pkg:
            pkg = _one(con, "SELECT * FROM brainlive_day_packages WHERE person_id=? ORDER BY created_at DESC LIMIT 1", (person_id,))
        live_payload = dict(pkg) if pkg else {"missing_package": True}
        brain2_payload = collect_brain2_forecast_evidence(person_id, limit=limit)
        recent_recs = _compact(_query(con, "SELECT * FROM brainlive_brain2_reconciliations WHERE person_id=? ORDER BY created_at DESC LIMIT 20", (person_id,)), 20)
    if use_llm:
        out, error = _run_llm(
            "Tu es le juge de coordination BrainLive↔Brain2. Compare ce que BrainLive a prédit/intervenu/observé avec les hypothèses et prédictions Brain2. Ne juge que ce qui est étayé par les données.",
            {"mission": "Crée des verdicts de réconciliation: confirmé, contredit, partiel, trop tôt/tard, opportunité manquée, silence utile. Explique le delta d'apprentissage pour Brain2 et BrainLive.", "brainlive_package": live_payload, "brain2_material": brain2_payload, "recent_reconciliations": recent_recs, "schema": RECONCILIATION_SCHEMA},
            RECONCILIATION_SCHEMA,
            timeout=timeout,
        )
        items = out.get("reconciliations") if not error and isinstance(out, dict) else []
        status = "llm_ready" if items else "raw_ready_llm_required"
    else:
        out = {"llm_required": True, "raw_evidence_available": True}
        items = []
        status = "raw_only_llm_disabled"
    now = now_iso(); created: list[str] = []
    with connect() as con:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            live_table = str(item.get("live_source_table") or "unknown")[:200]
            live_id = str(item.get("live_source_id") or stable_id("liveunknown", live_table, item))[:500]
            rec_id = stable_id("blb2rec", person_id, live_table, live_id, item.get("brain2_source_table"), item.get("brain2_source_id"), item.get("verdict"))
            upsert(con, "brainlive_brain2_reconciliations", {
                "reconciliation_id": rec_id, "person_id": person_id, "package_id": (pkg or {}).get("package_id") if isinstance(pkg, dict) else package_id,
                "live_source_table": live_table, "live_source_id": live_id,
                "brain2_source_table": item.get("brain2_source_table"), "brain2_source_id": item.get("brain2_source_id"),
                "verdict": item.get("verdict") or "unscored", "verdict_confidence": _clamp(item.get("verdict_confidence")),
                "what_brainlive_thought_json": json_dumps(item.get("what_brainlive_thought") or {}),
                "what_happened_json": json_dumps(item.get("what_happened") or {}),
                "what_brain2_knows_json": json_dumps(item.get("what_brain2_knows") or {}),
                "learning_delta_json": json_dumps(item.get("learning_delta") or {}), "status": "open",
                "created_at": now, "updated_at": now,
            }, "reconciliation_id")
            created.append(rec_id)
        con.commit()
    return {"version": VERSION, "person_id": person_id, "package_id": (pkg or {}).get("package_id") if isinstance(pkg, dict) else package_id, "status": status, "reconciliations_created": len(created), "reconciliation_ids": created, "llm_output": out}


def update_life_model_lifecycle(person_id: str = "me") -> dict[str, Any]:
    """Compute lifecycle/staleness metadata for canonical Brain2 model rows.

    No interpretation: recent rows stay active; old unconfirmed rows become stale;
    rows contradicted by reconciliation get contradiction markers. This gives
    BrainLive a recency/obsolescence layer without forcing Brain2 to rerun.
    """
    ensure_coordination_schema()
    now_dt = datetime.now(timezone.utc)
    now = now_iso(); upserts = 0
    with connect() as con:
        recs = _query(con, "SELECT * FROM brainlive_brain2_reconciliations WHERE person_id=? ORDER BY created_at DESC LIMIT 500", (person_id,))
        contradicted = {(r.get("brain2_source_table"), r.get("brain2_source_id")) for r in recs if str(r.get("verdict") or "").startswith("contradicted") or r.get("verdict") in {"wrong_context"}}
        confirmed = {(r.get("brain2_source_table"), r.get("brain2_source_id")) for r in recs if r.get("verdict") in {"confirmed", "partially_confirmed"}}
        for table, pk in CANONICAL_TABLES:
            if not _table_exists(con, table):
                continue
            rows = _query(con, f"SELECT * FROM {table} WHERE person_id=?", (person_id,))
            for r in rows:
                sid = r.get(pk)
                if not sid:
                    continue
                raw_time = r.get("updated_at") or r.get("created_at") or now
                try:
                    seen_dt = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                except Exception:
                    seen_dt = now_dt
                age_days = max(0.0, (now_dt - seen_dt).total_seconds() / 86400.0)
                staleness = min(1.0, age_days / 90.0)
                recency = max(0.0, 1.0 - staleness)
                key = (table, sid)
                validity = "active"
                obsolete_reason = None
                last_confirmed = None
                last_contradicted = None
                conf_delta = 0.0
                if key in contradicted:
                    validity = "contradicted_needs_review"; obsolete_reason = "recent_brainlive_brain2_reconciliation_contradicted"; last_contradicted = now; conf_delta = -0.25
                elif key in confirmed:
                    validity = "active_confirmed"; last_confirmed = now; conf_delta = 0.10
                elif staleness >= 0.85:
                    validity = "stale_needs_refresh"; obsolete_reason = "not_seen_recently"
                lifecycle_id = stable_id("b2lifeLC", person_id, table, sid)
                old = _one(con, "SELECT * FROM brain2_life_model_lifecycle WHERE lifecycle_id=?", (lifecycle_id,))
                first_seen = (old or {}).get("first_seen_at") or r.get("created_at") or now
                upsert(con, "brain2_life_model_lifecycle", {
                    "lifecycle_id": lifecycle_id, "person_id": person_id, "source_table": table, "source_id": sid,
                    "first_seen_at": first_seen, "last_seen_at": raw_time,
                    "last_confirmed_at": last_confirmed or (old or {}).get("last_confirmed_at"),
                    "last_contradicted_at": last_contradicted or (old or {}).get("last_contradicted_at"),
                    "recency_weight": recency, "staleness_score": staleness, "confidence_delta": conf_delta,
                    "validity_status": validity, "obsolete_reason": obsolete_reason,
                    "evidence_json": json_dumps({"source_confidence": r.get("confidence"), "source_status": r.get("status"), "age_days": round(age_days, 2)}),
                    "created_at": (old or {}).get("created_at") or now, "updated_at": now,
                }, "lifecycle_id")
                upserts += 1
        con.commit()
    return {"version": VERSION, "person_id": person_id, "lifecycle_rows_upserted": upserts}


def snapshot_live_context_for_audit(live_session_id: str | None, person_id: str = "me", *, source_table: str | None = None, source_id: str | None = None, snapshot_kind: str = "coordination") -> dict[str, Any]:
    ensure_coordination_schema()
    now = now_iso()
    with connect() as con:
        active_people: list[Any] = []
        if live_session_id and _table_exists(con, "brainlive_hot_identity_cache"):
            active_people = _query(con, "SELECT * FROM brainlive_hot_identity_cache WHERE live_session_id=? ORDER BY updated_at DESC LIMIT 5", (live_session_id,))
        place = _one(con, "SELECT * FROM brainlive_hot_context_cache WHERE live_session_id=? ORDER BY updated_at DESC LIMIT 1", (live_session_id,)) if live_session_id and _table_exists(con, "brainlive_hot_context_cache") else None
        life_model = {
            "hooks": _compact(_active_live_prediction_hooks(con, person_id, 40), 40),
            "lifecycle": _compact(_query(con, "SELECT * FROM brain2_life_model_lifecycle WHERE person_id=? ORDER BY updated_at DESC LIMIT 60", (person_id,)), 60),
        }
        bindings = _compact(_query(con, "SELECT * FROM brain2_live_watch_bindings WHERE person_id=? AND status='active' ORDER BY confidence DESC LIMIT 80", (person_id,)), 80)
        forecasts = collect_brain2_forecast_evidence(person_id, limit=40)
        state = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,)) if live_session_id and _table_exists(con, "brainlive_sessions") else None
        digest = stable_id("ctxdigest", person_id, live_session_id, json_dumps(bindings[:10]), json_dumps(life_model.get("hooks", [])[:10]))
        snapshot_id = stable_id("blctxsnap", person_id, live_session_id or "none", source_table, source_id, snapshot_kind, now)
        upsert(con, "brainlive_context_snapshots_v1512", {
            "snapshot_id": snapshot_id, "live_session_id": live_session_id, "person_id": person_id, "source_table": source_table, "source_id": source_id,
            "snapshot_kind": snapshot_kind, "active_people_json": json_dumps(active_people), "place_json": json_dumps(place or {}), "topic_json": json_dumps({}),
            "brain2_life_model_json": json_dumps(life_model), "watch_bindings_json": json_dumps(bindings), "brain2_forecasts_json": json_dumps(forecasts),
            "brainlive_state_json": json_dumps(state or {}), "digest": digest, "created_at": now,
        }, "snapshot_id")
        con.commit()
    return {"version": VERSION, "snapshot_id": snapshot_id, "digest": digest, "watch_bindings": len(bindings)}


def run_brainlive_brain2_coordination(person_id: str = "me", *, package_date: str | None = None, use_llm: bool = True, timeout: float = 180.0) -> dict[str, Any]:
    """Full coordination pass: BrainLive evidence -> Brain2, and Brain2 forecasts -> BrainLive."""
    ensure_coordination_schema()
    started = now_iso(); run_id = stable_id("b2blrun", person_id, package_date or _iso_date(), started)
    status = "started"; error_text = None
    try:
        package = create_brainlive_day_package(person_id, package_date=package_date, use_llm=use_llm, timeout=timeout, limit=200)
        bindings = compile_brain2_forecasts_to_live_bindings(person_id, use_llm=use_llm, timeout=timeout, limit=160)
        reconciliation = reconcile_brainlive_with_brain2(person_id, package_id=package.get("package_id"), use_llm=use_llm, timeout=timeout, limit=120)
        lifecycle = update_life_model_lifecycle(person_id)
        status = "ok"
        counts = {"day_package": package.get("source_counts", {}), "bindings_created": bindings.get("bindings_created"), "reconciliations_created": reconciliation.get("reconciliations_created"), "lifecycle_rows": lifecycle.get("lifecycle_rows_upserted")}
    except Exception as exc:
        status = "error"; error_text = str(exc)[:2000]
        package = {}; bindings = {}; reconciliation = {}; lifecycle = {}; counts = {}
    finished = now_iso()
    with connect() as con:
        upsert(con, "brain2_brainlive_coordination_runs", {
            "run_id": run_id, "person_id": person_id, "run_kind": "full_coordination", "package_id": package.get("package_id"),
            "counts_json": json_dumps(counts), "status": status, "error_text": error_text, "started_at": started, "finished_at": finished,
        }, "run_id")
        con.commit()
    if status == "error":
        raise RuntimeError(error_text or "coordination_error")
    return {"version": VERSION, "run_id": run_id, "status": status, "package": package, "bindings": bindings, "reconciliation": reconciliation, "lifecycle": lifecycle}


def coordination_audit(person_id: str = "me") -> dict[str, Any]:
    ensure_coordination_schema()
    with connect() as con:
        counts: dict[str, int] = {}
        for table in ["brainlive_day_packages", "brain2_live_watch_bindings", "brainlive_brain2_reconciliations", "brain2_life_model_lifecycle", "brainlive_context_snapshots_v1512", "brain2_brainlive_coordination_runs"]:
            counts[table] = int((_one(con, f"SELECT COUNT(*) AS c FROM {table} WHERE person_id=?" if table != "brainlive_context_snapshots_v1512" else f"SELECT COUNT(*) AS c FROM {table} WHERE person_id=?", (person_id,)) or {"c": 0}).get("c") or 0)
        latest_run = _one(con, "SELECT * FROM brain2_brainlive_coordination_runs WHERE person_id=? ORDER BY started_at DESC LIMIT 1", (person_id,))
    missing = [k for k, v in counts.items() if v == 0]
    return {"version": VERSION, "person_id": person_id, "counts": counts, "missing_or_empty": missing, "latest_run": latest_run, "verdict": "ready" if not missing else "needs_coordination_run_or_live_data"}

# V18 remediation: explicit owner scope and revocation reaches live bindings,
# canonical hooks, projection state and descendants.
from .v18_coordination import install as _install_v18_coordination
_globals_v18_coordination = _install_v18_coordination(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_coordination)
