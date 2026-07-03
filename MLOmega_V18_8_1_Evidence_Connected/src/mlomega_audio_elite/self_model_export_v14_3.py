from __future__ import annotations

"""V14.3 automatic Pattern Mirror consolidation + readable self-model export.

This layer does not replace V13/V14/V14.2. It adds two final-user behaviours:

1. flow-watch can trigger periodic consolidations automatically instead of asking
   the user to run day/week/month commands by hand.
2. the evolving self-model can be exported as Markdown/JSON files for human
   inspection: what the system currently thinks it knows, suspects, predicts,
   and does not know about the user.

No regex is used in this module. Cognitive interpretation remains Qwen/JSON-
contract based in V14; this module schedules, collects, exports, and audits.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, init_db, upsert
from .utils import json_dumps, json_loads, now_iso, sha256_bytes, stable_id
from .v18_legacy_forecasts import active_legacy_forecasts as _active_v14_forecasts

V14_3_VERSION = "14.3.0-self-model-scheduler-final"

V14_3_TABLES = {
    "v14_3_schedule_state",
    "v14_3_schedule_runs",
    "v14_3_self_model_exports",
    "v14_3_self_model_export_sections",
    "v14_3_contract_checks",
}

DEFAULT_PERIODS = ["hour", "day", "week", "month"]
PERIOD_SECONDS = {
    "hour": 60 * 60,
    "day": 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
    "month": 30 * 24 * 60 * 60,
    "quarter": 90 * 24 * 60 * 60,
    "year": 365 * 24 * 60 * 60,
    "all_time": 30 * 24 * 60 * 60,
}

SELF_MODEL_SECTION_ORDER = [
    "identity",
    "current_state",
    "today_week_month",
    "active_traits",
    "needs_values_fears",
    "words_and_expressions",
    "thoughts_and_preoccupations",
    "emotions_and_states",
    "choices_and_decisions",
    "actions_intentions_outcomes",
    "relationships_and_triggers",
    "interpersonal_state_mirror",
    "proactive_interventions",
    "clarification_inbox",
    "people_identity_hypotheses",
    "active_desires_questions_solutions",
    "loops_patterns_contradictions",
    "predictions_and_forecasts",
    "blindspots_and_unknowns",
    "evidence_index",
]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _as_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1").fetchone()
    return row["person_id"] if row else "me"


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params)]
    except Exception:
        return []


def _safe_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    return json_loads(str(value), default if default is not None else {})


def _compact_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        compact: dict[str, Any] = {}
        for k, v in row.items():
            if v is None:
                continue
            if isinstance(v, str) and len(v) > 1400:
                compact[k] = v[:1400] + "…"
            else:
                compact[k] = v
        out.append(compact)
    return out


def ensure_v14_3_schema() -> None:
    from .brain2_router_v14_2 import ensure_v14_2_schema
    ensure_v14_2_schema()
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_3_schedule_state(
                schedule_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                period TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                last_run_at TEXT,
                next_due_at TEXT,
                run_count INTEGER DEFAULT 0,
                last_status TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_3_schedule_runs(
                run_id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                period TEXT NOT NULL,
                status TEXT NOT NULL,
                v14_snapshot_id TEXT,
                error_text TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS v14_3_self_model_exports(
                export_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                format TEXT NOT NULL,
                scope TEXT NOT NULL,
                output_path TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                section_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_3_self_model_export_sections(
                section_id TEXT PRIMARY KEY,
                export_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                section_name TEXT NOT NULL,
                source_tables_json TEXT DEFAULT '[]',
                item_count INTEGER DEFAULT 0,
                confidence_hint REAL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_3_contract_checks(
                check_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v143_schedule_person ON v14_3_schedule_state(person_id, period, next_due_at);
            CREATE INDEX IF NOT EXISTS idx_v143_exports_person ON v14_3_self_model_exports(person_id, created_at);
            """
        )
        now = now_iso()
        for table in sorted(V14_3_TABLES):
            upsert(con, "v14_3_contract_checks", {
                "check_id": stable_id("v143check", table),
                "check_name": f"table:{table}",
                "status": "declared",
                "detail": "V14.3 automatic periodic consolidation and self-model export table.",
                "created_at": now,
            }, "check_id")
        con.commit()


def _period_due(con, *, person_id: str, period: str, now_dt: datetime, force: bool) -> tuple[bool, str, datetime]:
    seconds = int(PERIOD_SECONDS.get(period, PERIOD_SECONDS["day"]))
    sid = stable_id("v143schedule", person_id, period)
    row = con.execute("SELECT * FROM v14_3_schedule_state WHERE schedule_id=?", (sid,)).fetchone()
    if not row:
        next_due = now_dt
        upsert(con, "v14_3_schedule_state", {
            "schedule_id": sid,
            "person_id": person_id,
            "period": period,
            "interval_seconds": seconds,
            "last_run_at": None,
            "next_due_at": _as_iso(next_due),
            "run_count": 0,
            "last_status": None,
            "last_error": None,
            "created_at": _as_iso(now_dt),
            "updated_at": _as_iso(now_dt),
        }, "schedule_id")
        con.commit()
        return True, sid, next_due
    next_due = _parse_iso(row["next_due_at"]) or now_dt
    return bool(force or next_due <= now_dt), sid, next_due


def run_due_periodic_consolidations(*, person_id: str | None = None, periods: list[str] | None = None, force: bool = False, export_after: bool = True) -> dict[str, Any]:
    """Run due V14 periodic jobs automatically.

    This is safe to call after every ingested file. It will only launch Qwen-heavy
    consolidations when the period is due, unless force=True.
    """
    ensure_v14_3_schema()
    from .pattern_mirror_v14 import run_periodic_mirror

    now_dt = _utcnow()
    results: list[dict[str, Any]] = []
    periods = periods or DEFAULT_PERIODS
    with connect() as con:
        person_id = person_id or _default_user(con)
    for period in periods:
        if period not in PERIOD_SECONDS:
            continue
        with connect() as con:
            due, schedule_id, _next_due = _period_due(con, person_id=person_id, period=period, now_dt=now_dt, force=force)
        if not due:
            results.append({"period": period, "status": "skipped_not_due"})
            continue
        started = _as_iso(_utcnow())
        status = "ok"
        error_text = None
        payload: dict[str, Any] = {}
        snapshot_id = None
        try:
            payload = run_periodic_mirror(person_id=person_id, period=period)
            snapshot_id = payload.get("snapshot_id") if isinstance(payload, dict) else None
        except Exception as exc:
            status = "error"
            error_text = str(exc)[:2000]
            payload = {"error": error_text}
        finished_dt = _utcnow()
        finished = _as_iso(finished_dt)
        seconds = int(PERIOD_SECONDS.get(period, PERIOD_SECONDS["day"]))
        next_due_at = _as_iso(finished_dt + timedelta(seconds=seconds))
        run_id = stable_id("v143run", schedule_id, period, started, finished, status)
        with connect() as con:
            upsert(con, "v14_3_schedule_runs", {
                "run_id": run_id,
                "schedule_id": schedule_id,
                "person_id": person_id,
                "period": period,
                "status": status,
                "v14_snapshot_id": snapshot_id,
                "error_text": error_text,
                "started_at": started,
                "finished_at": finished,
                "payload_json": json_dumps(payload),
            }, "run_id")
            row = con.execute("SELECT run_count FROM v14_3_schedule_state WHERE schedule_id=?", (schedule_id,)).fetchone()
            run_count = int(row["run_count"] or 0) + 1 if row else 1
            upsert(con, "v14_3_schedule_state", {
                "schedule_id": schedule_id,
                "person_id": person_id,
                "period": period,
                "interval_seconds": seconds,
                "last_run_at": finished,
                "next_due_at": next_due_at,
                "run_count": run_count,
                "last_status": status,
                "last_error": error_text,
                "created_at": finished,
                "updated_at": finished,
            }, "schedule_id")
            con.commit()
        results.append({"period": period, "status": status, "run_id": run_id, "snapshot_id": snapshot_id, "next_due_at": next_due_at, "error": error_text})
    exports: list[dict[str, Any]] = []
    if export_after and any(r.get("status") == "ok" for r in results):
        for fmt in ["markdown", "json"]:
            try:
                exports.append(export_self_model(person_id=person_id, fmt=fmt, scope="auto_after_consolidation"))
            except Exception as exc:
                exports.append({"format": fmt, "status": "error", "error": str(exc)[:500]})
    return {"version": V14_3_VERSION, "person_id": person_id, "force": force, "results": results, "exports": exports}


def scheduler_status(*, person_id: str | None = None) -> dict[str, Any]:
    ensure_v14_3_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        state = _many(con, "SELECT * FROM v14_3_schedule_state WHERE person_id=? ORDER BY period", (person_id,))
        runs = _many(con, "SELECT * FROM v14_3_schedule_runs WHERE person_id=? ORDER BY started_at DESC LIMIT 20", (person_id,))
    return {"version": V14_3_VERSION, "person_id": person_id, "schedule_state": state, "recent_runs": runs}


def collect_self_model_bundle(*, person_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    """Collect the current readable self-model from the DB.

    This does not invent. It gathers what V13/V14 already stored: words,
    expressions, states, thoughts, choices, predictions, relationships, loops,
    blindspots and unknowns.
    """
    ensure_v14_3_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        identity = _many(con, "SELECT person_id, display_name, is_user, created_at FROM speaker_profiles WHERE person_id=? OR is_user=1 ORDER BY is_user DESC LIMIT 5", (person_id,))
        bundle = {
            "version": V14_3_VERSION,
            "person_id": person_id,
            "generated_at": now_iso(),
            "sections": {
                "identity": identity,
                "current_state": _compact_rows(_many(con, "SELECT * FROM internal_state_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
                "today_week_month": _compact_rows(_many(con, "SELECT * FROM v14_periodic_self_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, 20)), 20),
                "active_traits": _compact_rows(_many(con, "SELECT * FROM self_model_dimensions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_self_model_readings WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
                "needs_values_fears": _compact_rows(_many(con, "SELECT * FROM memory_facets WHERE facet_type IN ('need','value','risk','emotion','energy_state') ORDER BY created_at DESC LIMIT ?", (limit,)), limit),
                "words_and_expressions": _compact_rows(_many(con, "SELECT * FROM personal_language_patterns WHERE person_id=? ORDER BY frequency DESC, last_seen DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM phrase_templates WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
                "thoughts_and_preoccupations": _compact_rows(_many(con, "SELECT * FROM thought_hypotheses WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
                "emotions_and_states": _compact_rows(_many(con, "SELECT * FROM emotion_evidence WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM state_transitions WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
                "choices_and_decisions": _compact_rows(_many(con, "SELECT * FROM choice_episodes WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
                "actions_intentions_outcomes": _compact_rows(_many(con, "SELECT * FROM action_intentions WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM action_outcomes WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM latent_outcome_links ORDER BY created_at DESC LIMIT ?", (limit,)), limit),
                "relationships_and_triggers": _compact_rows(_many(con, "SELECT * FROM relationship_models WHERE person_a=? OR person_b=? ORDER BY updated_at DESC LIMIT ?", (person_id, person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_people_trigger_maps WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
                "interpersonal_state_mirror": _compact_rows(_many(con, "SELECT * FROM v14_6_other_person_state_snapshots WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_6_interpersonal_emotional_couplings WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_6_micro_interaction_impacts WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_6_social_aftereffects WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_6_relationship_state_models WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_6_interpersonal_loop_cards WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_6_intervention_suggestions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_6_person_model_summaries WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
                "proactive_interventions": _compact_rows(_many(con, "SELECT * FROM v14_7_intervention_queue WHERE person_id=? AND status IN ('ready','pending','snoozed') ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_7_intervention_opportunities WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
                "clarification_inbox": _compact_rows(_many(con, "SELECT * FROM v14_8_clarification_items WHERE person_id=? AND status IN ('queued','watching','needs_followup') ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_8_clarification_answers WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
                "people_identity_hypotheses": _compact_rows(_many(con, "SELECT * FROM v14_5_people_identity_hypotheses ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_5_relationship_inference_cards ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_5_people_context_profiles ORDER BY updated_at DESC LIMIT ?", (limit,)), limit),
                "active_desires_questions_solutions": _compact_rows(_many(con, "SELECT * FROM v14_5_personal_open_loops WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_5_active_questions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_5_solution_candidates WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_5_next_best_actions WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
                "loops_patterns_contradictions": _compact_rows(_many(con, "SELECT * FROM loop_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_repetition_chains WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM contradiction_events WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
                "predictions_and_forecasts": _compact_rows(_many(con, "SELECT * FROM predictions WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_active_v14_forecasts(con, person_id=person_id, source_table="v14_trajectory_forecasts", limit=limit), limit) + _compact_rows(_active_v14_forecasts(con, person_id=person_id, source_table="v14_forecast_watch_queue", limit=limit), limit),
                "verification_calibration_revisions": _compact_rows(_many(con, "SELECT pr.* FROM prediction_results pr JOIN predictions p ON p.prediction_id=pr.prediction_id WHERE p.person_id=? ORDER BY pr.verified_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM calibration_scores WHERE person_id=? ORDER BY calculated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v13_replay_events WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM model_revisions WHERE target_table IN ('predictions','self_model','patterns') ORDER BY created_at DESC LIMIT ?", (limit,)), limit),
                "blindspots_and_unknowns": _compact_rows(_many(con, "SELECT * FROM v14_blindspot_hypotheses WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact_rows(_many(con, "SELECT * FROM v14_open_questions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
                "evidence_index": _compact_rows(_many(con, "SELECT * FROM v14_memory_horizon_index WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
            },
        }
    return bundle


def _section_title(name: str) -> str:
    titles = {
        "identity": "Identité centrale",
        "current_state": "État actuel inféré",
        "today_week_month": "Aujourd'hui / cette semaine / ce mois",
        "active_traits": "Traits actifs et dimensions du self-model",
        "needs_values_fears": "Besoins, valeurs, risques, peurs",
        "words_and_expressions": "Mots, expressions, tics de langage",
        "thoughts_and_preoccupations": "Pensées probables et préoccupations",
        "emotions_and_states": "Émotions et transitions d'état",
        "choices_and_decisions": "Choix, décisions, options rejetées",
        "actions_intentions_outcomes": "Intentions, actions, résultats, choses en attente",
        "relationships_and_triggers": "Relations et personnes déclencheuses",
        "interpersonal_state_mirror": "Miroir interpersonnel: états des autres, contagion émotionnelle, aftereffects",
        "proactive_interventions": "Interventions proactives: alertes, timing, actions à faire maintenant",
        "clarification_inbox": "Questions de clarification rares: identités, relations, corrections et réponses naturelles",
        "people_identity_hypotheses": "Hypothèses d'identité, liens familiaux/proximité, profils relationnels",
        "active_desires_questions_solutions": "Désirs, questions ouvertes, blocages, solutions candidates",
        "loops_patterns_contradictions": "Boucles, patterns, contradictions",
        "predictions_and_forecasts": "Prédictions, forecasts, trajectoires à surveiller",
        "verification_calibration_revisions": "Vérifications, calibration, révisions du modèle",
        "blindspots_and_unknowns": "Angles morts, questions ouvertes, ce que le système ne sait pas",
        "evidence_index": "Index de preuves et liens longs",
    }
    return titles.get(name, name.replace("_", " ").title())


def render_self_model_markdown(bundle: dict[str, Any]) -> str:
    person_id = bundle.get("person_id") or "me"
    lines: list[str] = []
    lines.append(f"# Self Model / Pattern Mirror — {person_id}")
    lines.append("")
    lines.append(f"Généré: {bundle.get('generated_at')}")
    lines.append("")
    lines.append("> Ce fichier n'est pas une vérité absolue. C'est le miroir actuel du système: faits, hypothèses, patterns, contre-preuves et inconnues à partir des données ingérées.")
    lines.append("")
    sections = bundle.get("sections") or {}
    for name in SELF_MODEL_SECTION_ORDER:
        items = sections.get(name) or []
        lines.append(f"## {_section_title(name)}")
        lines.append("")
        if not items:
            lines.append("_Aucun élément consolidé pour l'instant._")
            lines.append("")
            continue
        for i, item in enumerate(items[:25], 1):
            if not isinstance(item, dict):
                lines.append(f"{i}. {item}")
                continue
            label = item.get("title") or item.get("dimension") or item.get("statement") or item.get("summary") or item.get("overall_reading") or item.get("forecast_text") or item.get("expression") or item.get("thought_type") or item.get("choice_context") or item.get("intention_text") or item.get("hidden_pattern_summary") or item.get("theme") or item.get("question") or item.get("suspected_display_name") or item.get("other_person_hint") or item.get("person_hint") or item.get("action_text") or item.get("solution_summary") or item.get("moment_state_summary") or item.get("relationship_state_summary") or item.get("loop_title") or item.get("trigger_event_summary") or item.get("suggestion") or item.get("message") or item.get("recommended_action") or item.get("why_now") or item.get("summary") or item.get("display_name") or item.get("person_id") or f"élément {i}"
            conf = item.get("confidence") or item.get("score") or item.get("probability")
            suffix = f" — confiance/proba: {conf}" if conf is not None else ""
            lines.append(f"{i}. **{str(label)[:220]}**{suffix}")
            for key in ["state_summary", "hidden_pattern_summary", "summary", "statement", "possible_future_if_unchanged", "escape_condition", "warning_signal", "likely_next_step", "suspected_relation_to_user", "familiarity_level", "why_it_matters", "current_best_hypothesis", "why_this_might_work", "expected_effect", "risk_if_not_done", "source_state", "target_state_after", "coupling_type", "impact_direction", "impact_strength", "possible_aftereffect", "user_shift", "how_user_affects_them_json", "how_they_affect_user_json", "usual_outcome", "escape_conditions_json", "why_it_might_help", "risk_if_used_wrong", "message", "recommended_action", "why_now", "risk_if_ignored", "priority", "timing", "observed_evidence", "evidence_text", "result", "status", "updated_at", "created_at"]:
                value = item.get(key)
                if value:
                    lines.append(f"   - {key}: {str(value)[:700]}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def export_self_model(*, person_id: str | None = None, fmt: str = "markdown", scope: str = "full", output_dir: Path | None = None, limit: int = 80) -> dict[str, Any]:
    ensure_v14_3_schema()
    bundle = collect_self_model_bundle(person_id=person_id, limit=limit)
    person_id = str(bundle.get("person_id") or "me")
    settings = get_settings()
    out_dir = Path(output_dir or (settings.root_dir / "exports")).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt_norm = "json" if str(fmt).lower() == "json" else "markdown"
    suffix = "json" if fmt_norm == "json" else "md"
    timestamp = now_iso().replace(":", "-")
    filename = f"self_model_{person_id}_{timestamp}.{suffix}"
    path = out_dir / filename
    if fmt_norm == "json":
        content = json_dumps(bundle)
    else:
        content = render_self_model_markdown(bundle)
    path.write_text(content, encoding="utf-8")
    digest = sha256_bytes(content.encode("utf-8"))
    export_id = stable_id("v143export", person_id, fmt_norm, scope, digest)
    sections = bundle.get("sections") or {}
    with connect() as con:
        upsert(con, "v14_3_self_model_exports", {
            "export_id": export_id,
            "person_id": person_id,
            "format": fmt_norm,
            "scope": scope,
            "output_path": str(path),
            "content_sha256": digest,
            "section_count": len(sections),
            "created_at": now_iso(),
        }, "export_id")
        for name, items in sections.items():
            section_id = stable_id("v143section", export_id, name)
            upsert(con, "v14_3_self_model_export_sections", {
                "section_id": section_id,
                "export_id": export_id,
                "person_id": person_id,
                "section_name": name,
                "source_tables_json": json_dumps([]),
                "item_count": len(items) if isinstance(items, list) else 1,
                "confidence_hint": 0.0,
                "created_at": now_iso(),
            }, "section_id")
        con.commit()
    return {"version": V14_3_VERSION, "export_id": export_id, "person_id": person_id, "format": fmt_norm, "path": str(path), "section_count": len(sections)}


def audit_v14_3(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_3_schema()
    with connect() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(V14_3_TABLES - tables)
        counts: dict[str, int | None] = {}
        for table in sorted(V14_3_TABLES):
            try:
                counts[table] = int(con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
            except Exception:
                counts[table] = None
    return {
        "version": V14_3_VERSION,
        "ok": not missing,
        "missing_tables": missing,
        "required_tables": sorted(V14_3_TABLES),
        "counts": counts,
        "automatic_schedule": {
            "flow_watch_calls_scheduler": True,
            "default_periods": DEFAULT_PERIODS,
            "manual_force_command": "mlomega-audio v14-auto-consolidate --force",
        },
        "self_model_export": {
            "markdown": "mlomega-audio export-self-model --person-id me --format markdown",
            "json": "mlomega-audio export-self-model --person-id me --format json",
            "sections": SELF_MODEL_SECTION_ORDER,
            "v14_5_sections": ["people_identity_hypotheses", "active_desires_questions_solutions"],
            "v14_6_sections": ["interpersonal_state_mirror"],
            "v14_7_sections": ["proactive_interventions"],
            "v14_8_sections": ["clarification_inbox"],
        },
    }
