from __future__ import annotations

"""V14.7 Proactive Intervention Layer.

This layer turns Brain2 from a mirror into a timing-aware companion.
It does not replace raw memory, V13 prediction, V14 mirror, V14.2 vector
fusion, V14.3 scheduler, V14.4 auto-verification, V14.5 people/open-loop
tracking or V14.6 interpersonal state modelling.

Its job is to decide when an insight should become an actionable intervention:
- right after a tense exchange;
- after a positive micro-interaction that creates a good action window;
- when an old loop is active again;
- before the user makes a likely repeated mistake;
- when a desire/open question has enough evidence for a next step;
- when a forecast needs watching rather than another passive report.

It stores hypotheses and queued interventions with evidence, counter-evidence,
urgency, timing, cooldown, expiry, feedback and later outcome tracking. It does
not send push notifications by itself; it exposes an intervention inbox and
export files so the host app / phone bridge can decide how to notify the user.
Cognitive decisions are made by Qwen/Ollama JSON contracts. Local code only
stores, ranks, suppresses duplicates and tracks user feedback.
"""

from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, init_db, upsert
from .interpersonal_state_v14_6 import ensure_v14_6_schema
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v18_legacy_forecasts import active_legacy_forecasts as _active_v14_forecasts

V14_7_VERSION = "14.7.0-proactive-intervention-final"

V14_7_TABLES = {
    "v14_7_intervention_runs",
    "v14_7_intervention_opportunities",
    "v14_7_intervention_queue",
    "v14_7_intervention_feedback",
    "v14_7_intervention_outcomes",
    "v14_7_intervention_policies",
    "v14_7_intervention_exports",
    "v14_7_contract_checks",
}

INTERVENTION_SCHEMA: dict[str, Any] = {
    "opportunities": [
        {
            "title": "",
            "category": "loop_interrupt|social_aftereffect|positive_window|decision_guard|open_loop_next_step|prediction_watch|relationship_repair|energy_guard|focus_guard|self_model_update|other|unknown",
            "urgency": "low|medium|high|critical",
            "timing": "now|soon|today|before_next_action|after_pause|weekly_review|watch_only|unknown",
            "should_notify": False,
            "intervention_message": "",
            "recommended_action": "",
            "why_now": "",
            "risk_if_ignored": "",
            "possible_harm_if_overused": "",
            "evidence": [],
            "counter_evidence": [],
            "source_tables": [],
            "source_ids": [],
            "linked_person_hint": None,
            "linked_domain": "personal|relationship|professional|project|mood|energy|decision|unknown|other",
            "cooldown_key": "",
            "expiry_horizon": "minutes|hours|day|week|month|open|unknown",
            "confidence": 0.0,
        }
    ],
    "daily_top_three": [
        {
            "title": "",
            "why_it_matters_today": "",
            "recommended_action": "",
            "confidence": 0.0,
        }
    ],
    "do_not_interrupt": [],
    "missing_context": [],
    "confidence": 0.0,
}

DEFAULT_POLICY = {
    "min_notify_confidence": 0.58,
    "min_queue_confidence": 0.42,
    "max_new_notifications_per_run": 5,
    "allow_critical": True,
    "default_channel": "inbox_file",
    "cooldown_policy": "one_open_item_per_cooldown_key",
    "requires_human_review_for_identity": True,
    "urgent_allowed_categories": [
        "loop_interrupt",
        "decision_guard",
        "relationship_repair",
        "energy_guard",
        "focus_guard",
        "social_aftereffect",
    ],
}


def _clamp(v: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        f = float(v)
    except Exception:
        f = 0.0
    return max(lo, min(hi, f))


def _safe_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    return json_loads(str(value), default if default is not None else {})


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params)]
    except Exception:
        return []


def _one_value(con, sql: str, params: tuple[Any, ...] = (), default: Any = None) -> Any:
    try:
        row = con.execute(sql, params).fetchone()
        if row is None:
            return default
        return list(dict(row).values())[0]
    except Exception:
        return default


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["person_id"] if row else "me"


def _compact(rows: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
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


def _llm_json(system: str, payload: dict[str, Any], schema: dict[str, Any], timeout: int = 480) -> dict[str, Any]:
    data = OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=timeout)
    if not isinstance(data, dict):
        raise RuntimeError("Brain2 V14.7 returned non-object JSON")
    return data


def ensure_v14_7_schema() -> None:
    init_db()
    ensure_v14_6_schema()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_7_intervention_runs(
                run_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                conversation_id TEXT,
                trigger_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_count INTEGER DEFAULT 0,
                queued_count INTEGER DEFAULT 0,
                suppressed_count INTEGER DEFAULT 0,
                qwen_json TEXT DEFAULT '{}',
                error_text TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_7_intervention_opportunities(
                opportunity_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                conversation_id TEXT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                urgency TEXT NOT NULL,
                timing TEXT NOT NULL,
                should_notify INTEGER DEFAULT 0,
                intervention_message TEXT NOT NULL,
                recommended_action TEXT,
                why_now TEXT,
                risk_if_ignored TEXT,
                possible_harm_if_overused TEXT,
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                source_tables_json TEXT DEFAULT '[]',
                source_ids_json TEXT DEFAULT '[]',
                linked_person_hint TEXT,
                linked_domain TEXT,
                cooldown_key TEXT,
                expiry_horizon TEXT,
                confidence REAL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_7_intervention_queue(
                queue_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                conversation_id TEXT,
                title TEXT NOT NULL,
                priority TEXT NOT NULL,
                timing TEXT NOT NULL,
                channel TEXT NOT NULL,
                message TEXT NOT NULL,
                recommended_action TEXT,
                why_now TEXT,
                cooldown_key TEXT,
                status TEXT NOT NULL,
                due_at TEXT,
                expires_at TEXT,
                delivered_at TEXT,
                snoozed_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_7_intervention_feedback(
                feedback_id TEXT PRIMARY KEY,
                queue_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                feedback_type TEXT NOT NULL,
                feedback_note TEXT,
                helpfulness REAL,
                action_taken TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_7_intervention_outcomes(
                outcome_id TEXT PRIMARY KEY,
                queue_id TEXT NOT NULL,
                opportunity_id TEXT,
                person_id TEXT NOT NULL,
                observed_later_summary TEXT,
                outcome_type TEXT,
                did_help INTEGER,
                evidence_json TEXT DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_7_intervention_policies(
                person_id TEXT PRIMARY KEY,
                policy_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_7_intervention_exports(
                export_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                path TEXT NOT NULL,
                pending_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_7_contract_checks(
                check_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v147_queue_person_status ON v14_7_intervention_queue(person_id, status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_v147_opp_person_status ON v14_7_intervention_opportunities(person_id, status, confidence, created_at);
            CREATE INDEX IF NOT EXISTS idx_v147_opp_cooldown ON v14_7_intervention_opportunities(person_id, cooldown_key, status);
            CREATE INDEX IF NOT EXISTS idx_v147_feedback_queue ON v14_7_intervention_feedback(queue_id, created_at);
            """
        )
        now = now_iso()
        for table in sorted(V14_7_TABLES):
            upsert(con, "v14_7_contract_checks", {
                "check_id": stable_id("v147check", table),
                "check_name": f"exists:{table}",
                "status": "ok",
                "details_json": json_dumps({"table": table, "version": V14_7_VERSION}),
                "created_at": now,
            }, "check_id")
        con.commit()


def get_intervention_policy(person_id: str | None = None) -> dict[str, Any]:
    ensure_v14_7_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        row = con.execute("SELECT policy_json FROM v14_7_intervention_policies WHERE person_id=?", (person_id,)).fetchone()
        if row:
            policy = _safe_json(row["policy_json"], {}) or {}
            merged = dict(DEFAULT_POLICY)
            merged.update(policy)
            return {"person_id": person_id, "policy": merged}
        now = now_iso()
        upsert(con, "v14_7_intervention_policies", {
            "person_id": person_id,
            "policy_json": json_dumps(DEFAULT_POLICY),
            "updated_at": now,
        }, "person_id")
        con.commit()
    return {"person_id": person_id, "policy": dict(DEFAULT_POLICY)}


def update_intervention_policy(person_id: str | None = None, *, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_v14_7_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        current = get_intervention_policy(person_id)["policy"]
        if patch:
            current.update(patch)
        now = now_iso()
        upsert(con, "v14_7_intervention_policies", {
            "person_id": person_id,
            "policy_json": json_dumps(current),
            "updated_at": now,
        }, "person_id")
        con.commit()
    return {"person_id": person_id, "policy": current}


def _context_for_intervention(con, *, person_id: str, conversation_id: str | None = None, limit: int = 30) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    if conversation_id:
        turns = _many(con, "SELECT * FROM turns WHERE conversation_id=? ORDER BY idx LIMIT ?", (conversation_id, 80))
        episodes = _many(con, "SELECT * FROM episodes WHERE source_conversation_id=? ORDER BY created_at DESC LIMIT ?", (conversation_id, limit))
    from .v18_brain2_context import conversation_context_addenda
    context_addenda = (conversation_context_addenda(con, conversation_id=conversation_id, person_id=person_id) if conversation_id else {"entries": [], "budget": {"context_incomplete": False}})
    return {
        "conversation_id": conversation_id,
        "recent_turns": _compact(turns, 80),
        "context_addenda": context_addenda,
        "recent_episodes": _compact(episodes, limit),
        "interpersonal_aftereffects": _compact(_many(con, "SELECT * FROM v14_6_social_aftereffects WHERE person_id=? AND status='open' ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "interpersonal_loops": _compact(_many(con, "SELECT * FROM v14_6_interpersonal_loop_cards WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "interpersonal_suggestions": _compact(_many(con, "SELECT * FROM v14_6_intervention_suggestions WHERE person_id=? ORDER BY confidence DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
        "active_open_loops": _compact(_many(con, "SELECT * FROM v14_5_personal_open_loops WHERE person_id=? AND current_status IN ('open','active','pending','watching') ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "solution_candidates": _compact(_many(con, "SELECT * FROM v14_5_solution_candidates WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "pattern_cards": _compact(_many(con, "SELECT * FROM v14_pattern_mirror_cards WHERE person_id=? AND status='open' ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "trajectory_forecasts": _compact(_active_v14_forecasts(con, person_id, "v14_trajectory_forecasts", limit), limit),
        "forecast_watch_queue": _compact(_active_v14_forecasts(con, person_id, "v14_forecast_watch_queue", limit), limit),
        "recent_predictions": _compact(_many(con, "SELECT * FROM predictions WHERE person_id=? AND status IN ('open','active','watch') ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
        "recent_prediction_results": _compact(_many(con, "SELECT pr.* FROM prediction_results pr JOIN predictions p ON p.prediction_id=pr.prediction_id WHERE p.person_id=? ORDER BY pr.verified_at DESC LIMIT ?", (person_id, limit)), limit),
        "recent_state": _compact(_many(con, "SELECT * FROM internal_state_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
        "existing_open_queue": _compact(_many(con, "SELECT * FROM v14_7_intervention_queue WHERE person_id=? AND status IN ('pending','ready','snoozed') ORDER BY created_at DESC LIMIT ?", (person_id, 30)), 30),
        "policy": get_intervention_policy(person_id)["policy"],
    }


def _severity_score(urgency: str, confidence: float, should_notify: bool) -> float:
    weights = {"low": 0.20, "medium": 0.45, "high": 0.72, "critical": 0.92}
    return _clamp(weights.get(str(urgency), 0.30) + 0.25 * confidence + (0.08 if should_notify else 0.0))


def _priority(urgency: str, confidence: float, should_notify: bool) -> str:
    score = _severity_score(urgency, confidence, should_notify)
    if score >= 0.88:
        return "critical"
    if score >= 0.68:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _queue_status(timing: str, should_notify: bool, confidence: float, policy: dict[str, Any]) -> str:
    min_notify = float(policy.get("min_notify_confidence") or DEFAULT_POLICY["min_notify_confidence"])
    if should_notify and confidence >= min_notify and str(timing) not in {"watch_only", "weekly_review"}:
        return "ready"
    return "pending"


def _has_open_cooldown(con, *, person_id: str, cooldown_key: str, opportunity_id: str | None = None) -> bool:
    if not cooldown_key:
        return False
    rows = _many(con, "SELECT opportunity_id FROM v14_7_intervention_opportunities WHERE person_id=? AND cooldown_key=? AND status IN ('queued','open') LIMIT 5", (person_id, cooldown_key))
    ids = {str(r.get("opportunity_id")) for r in rows}
    if opportunity_id and opportunity_id in ids and len(ids) == 1:
        return False
    return bool(ids)


def run_proactive_interventions(conversation_id: str | None = None, *, person_id: str | None = None, trigger_type: str = "direct_flow", limit: int = 30) -> dict[str, Any]:
    ensure_v14_7_schema()
    run_id = stable_id("v147run", person_id or "auto", conversation_id or "global", trigger_type, now_iso())
    now = now_iso()
    status = "ok"
    error_text = None
    qwen_json: dict[str, Any] = {}
    created_count = 0
    queued_count = 0
    suppressed_count = 0
    with connect() as con:
        person_id = person_id or _default_user(con)
        policy = get_intervention_policy(person_id)["policy"]
        context = _context_for_intervention(con, person_id=person_id, conversation_id=conversation_id, limit=limit)
        payload = {
            "mission": "Décide seulement quelles observations doivent devenir des interventions proactives. Ne crée pas de notification pour chaque insight. Priorise le bon timing: interrompre une boucle, protéger une décision, utiliser une fenêtre positive, rappeler une petite action, ou surveiller sans notifier. Réponds en hypothèses avec preuves, contre-preuves, risque si ignoré et risque si l'intervention est trop intrusive.",
            "person_id": person_id,
            "conversation_id": conversation_id,
            "trigger_type": trigger_type,
            "context": context,
            "policy": policy,
            "schema": INTERVENTION_SCHEMA,
        }
        try:
            qwen_json = _llm_json("Tu es Brain2 V14.7 Proactive Intervention. Réponds uniquement en JSON valide.", payload, INTERVENTION_SCHEMA, timeout=480)
        except Exception as exc:
            status = "error"
            error_text = str(exc)[:2000]
            upsert(con, "v14_7_intervention_runs", {
                "run_id": run_id,
                "person_id": person_id,
                "conversation_id": conversation_id,
                "trigger_type": trigger_type,
                "status": status,
                "created_count": 0,
                "queued_count": 0,
                "suppressed_count": 0,
                "qwen_json": json_dumps({}),
                "error_text": error_text,
                "created_at": now,
            }, "run_id")
            con.commit()
            return {"version": V14_7_VERSION, "run_id": run_id, "status": status, "error_text": error_text, "created_count": 0, "queued_count": 0, "suppressed_count": 0}
        opportunities = qwen_json.get("opportunities") if isinstance(qwen_json.get("opportunities"), list) else []
        for idx, item in enumerate(opportunities):
            if not isinstance(item, dict):
                continue
            confidence = _clamp(item.get("confidence"))
            min_queue = float(policy.get("min_queue_confidence") or DEFAULT_POLICY["min_queue_confidence"])
            if confidence < min_queue:
                suppressed_count += 1
                continue
            title = str(item.get("title") or item.get("intervention_message") or "Intervention proposée")[:300]
            category = str(item.get("category") or "unknown")
            urgency = str(item.get("urgency") or "low")
            timing = str(item.get("timing") or "watch_only")
            should_notify = 1 if bool(item.get("should_notify")) else 0
            cooldown_key = str(item.get("cooldown_key") or stable_id("cooldown", person_id, category, item.get("linked_person_hint") or "", title[:80]))
            opportunity_id = stable_id("v147opp", person_id, category, cooldown_key, title, item.get("source_ids") or [], conversation_id or "")
            if _has_open_cooldown(con, person_id=person_id, cooldown_key=cooldown_key, opportunity_id=opportunity_id):
                suppressed_count += 1
                continue
            created_count += 1
            priority = _priority(urgency, confidence, bool(should_notify))
            opp_status = "queued"
            upsert(con, "v14_7_intervention_opportunities", {
                "opportunity_id": opportunity_id,
                "run_id": run_id,
                "person_id": person_id,
                "conversation_id": conversation_id,
                "title": title,
                "category": category,
                "urgency": urgency,
                "timing": timing,
                "should_notify": should_notify,
                "intervention_message": str(item.get("intervention_message") or title),
                "recommended_action": item.get("recommended_action"),
                "why_now": item.get("why_now"),
                "risk_if_ignored": item.get("risk_if_ignored"),
                "possible_harm_if_overused": item.get("possible_harm_if_overused"),
                "evidence_json": json_dumps(item.get("evidence") if isinstance(item.get("evidence"), list) else []),
                "counter_evidence_json": json_dumps(item.get("counter_evidence") if isinstance(item.get("counter_evidence"), list) else []),
                "source_tables_json": json_dumps(item.get("source_tables") if isinstance(item.get("source_tables"), list) else []),
                "source_ids_json": json_dumps(item.get("source_ids") if isinstance(item.get("source_ids"), list) else []),
                "linked_person_hint": item.get("linked_person_hint"),
                "linked_domain": item.get("linked_domain") or "unknown",
                "cooldown_key": cooldown_key,
                "expiry_horizon": item.get("expiry_horizon") or "unknown",
                "confidence": confidence,
                "status": opp_status,
                "created_at": now,
                "updated_at": now,
            }, "opportunity_id")
            queue_id = stable_id("v147queue", opportunity_id, person_id, cooldown_key)
            q_status = _queue_status(timing, bool(should_notify), confidence, policy)
            upsert(con, "v14_7_intervention_queue", {
                "queue_id": queue_id,
                "opportunity_id": opportunity_id,
                "person_id": person_id,
                "conversation_id": conversation_id,
                "title": title,
                "priority": priority,
                "timing": timing,
                "channel": str(policy.get("default_channel") or "inbox_file"),
                "message": str(item.get("intervention_message") or title),
                "recommended_action": item.get("recommended_action"),
                "why_now": item.get("why_now"),
                "cooldown_key": cooldown_key,
                "status": q_status,
                "due_at": None,
                "expires_at": None,
                "delivered_at": None,
                "snoozed_until": None,
                "created_at": now,
                "updated_at": now,
            }, "queue_id")
            queued_count += 1
        upsert(con, "v14_7_intervention_runs", {
            "run_id": run_id,
            "person_id": person_id,
            "conversation_id": conversation_id,
            "trigger_type": trigger_type,
            "status": status,
            "created_count": created_count,
            "queued_count": queued_count,
            "suppressed_count": suppressed_count,
            "qwen_json": json_dumps(qwen_json),
            "error_text": error_text,
            "created_at": now,
        }, "run_id")
        con.commit()
    export_intervention_inbox(person_id=person_id, limit=20)
    return {"version": V14_7_VERSION, "run_id": run_id, "person_id": person_id, "conversation_id": conversation_id, "status": status, "created_count": created_count, "queued_count": queued_count, "suppressed_count": suppressed_count, "top_three": qwen_json.get("daily_top_three", [])}


def list_intervention_inbox(person_id: str | None = None, *, status: str | None = None, priority: str | None = None, limit: int = 30) -> dict[str, Any]:
    ensure_v14_7_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        params: list[Any] = [person_id]
        clauses = ["person_id=?"]
        if status:
            clauses.append("status=?")
            params.append(status)
        else:
            clauses.append("status IN ('ready','pending','snoozed')")
        if priority:
            clauses.append("priority=?")
            params.append(priority)
        sql = "SELECT * FROM v14_7_intervention_queue WHERE " + " AND ".join(clauses) + " ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC LIMIT ?"
        params.append(limit)
        rows = _many(con, sql, tuple(params))
    return {"version": V14_7_VERSION, "person_id": person_id, "count": len(rows), "items": rows}


def record_intervention_feedback(queue_id: str, *, person_id: str | None = None, feedback_type: str = "dismissed", note: str | None = None, helpfulness: float | None = None, action_taken: str | None = None) -> dict[str, Any]:
    ensure_v14_7_schema()
    now = now_iso()
    with connect() as con:
        row = con.execute("SELECT * FROM v14_7_intervention_queue WHERE queue_id=?", (queue_id,)).fetchone()
        if not row:
            return {"version": V14_7_VERSION, "status": "not_found", "queue_id": queue_id}
        rowd = dict(row)
        person_id = person_id or rowd.get("person_id") or _default_user(con)
        feedback_id = stable_id("v147fb", queue_id, feedback_type, note or "", now)
        upsert(con, "v14_7_intervention_feedback", {
            "feedback_id": feedback_id,
            "queue_id": queue_id,
            "person_id": person_id,
            "feedback_type": feedback_type,
            "feedback_note": note,
            "helpfulness": helpfulness,
            "action_taken": action_taken,
            "created_at": now,
        }, "feedback_id")
        status_map = {
            "dismissed": "dismissed",
            "acted": "acted",
            "helpful": "acted",
            "not_relevant": "dismissed",
            "too_intrusive": "dismissed",
            "snoozed": "snoozed",
            "delivered": "delivered",
        }
        new_status = status_map.get(feedback_type, rowd.get("status") or "pending")
        con.execute("UPDATE v14_7_intervention_queue SET status=?, delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END, updated_at=? WHERE queue_id=?", (new_status, feedback_type, now, now, queue_id))
        con.execute("UPDATE v14_7_intervention_opportunities SET status=? , updated_at=? WHERE opportunity_id=?", ("closed" if new_status in {"dismissed", "acted"} else "queued", now, rowd.get("opportunity_id")))
        con.commit()
    return {"version": V14_7_VERSION, "status": "ok", "queue_id": queue_id, "feedback_id": feedback_id, "new_status": new_status}


def export_intervention_inbox(person_id: str | None = None, *, output_dir: Path | None = None, limit: int = 20) -> dict[str, Any]:
    ensure_v14_7_schema()
    settings = get_settings()
    out_dir = Path(output_dir or (settings.root_dir / "exports")).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        person_id = person_id or _default_user(con)
        rows = _many(con, "SELECT * FROM v14_7_intervention_queue WHERE person_id=? AND status IN ('ready','pending','snoozed') ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC LIMIT ?", (person_id, limit))
        now = now_iso()
        path = out_dir / f"intervention_inbox_{person_id}.md"
        lines = [f"# Intervention Inbox — {person_id}", "", f"Generated: {now}", ""]
        if not rows:
            lines.append("Aucune intervention ouverte.")
        for i, row in enumerate(rows, 1):
            lines += [
                f"## {i}. [{row.get('priority')}] {row.get('title')}",
                "",
                f"Status: {row.get('status')} | Timing: {row.get('timing')} | Conversation: {row.get('conversation_id') or '?'}",
                "",
                str(row.get("message") or ""),
                "",
            ]
            if row.get("recommended_action"):
                lines += [f"Action: {row.get('recommended_action')}", ""]
            if row.get("why_now"):
                lines += [f"Pourquoi maintenant: {row.get('why_now')}", ""]
            lines += [f"Queue ID: `{row.get('queue_id')}`", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        export_id = stable_id("v147export", person_id, str(path), now)
        upsert(con, "v14_7_intervention_exports", {
            "export_id": export_id,
            "person_id": person_id,
            "path": str(path),
            "pending_count": len(rows),
            "created_at": now,
        }, "export_id")
        con.commit()
    return {"version": V14_7_VERSION, "person_id": person_id, "path": str(path), "pending_count": len(rows)}


def audit_v14_7(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_7_schema()
    with connect() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(V14_7_TABLES - tables)
        counts: dict[str, Any] = {}
        for t in sorted(V14_7_TABLES):
            counts[t] = _one_value(con, f"SELECT COUNT(*) AS c FROM {t}", default="missing")
        if persist:
            now = now_iso()
            for t in sorted(V14_7_TABLES):
                upsert(con, "v14_7_contract_checks", {
                    "check_id": stable_id("v147audit", t),
                    "check_name": f"exists:{t}",
                    "status": "ok" if t in tables else "missing",
                    "details_json": json_dumps({"table": t, "version": V14_7_VERSION}),
                    "created_at": now,
                }, "check_id")
            con.commit()
    return {
        "version": V14_7_VERSION,
        "ok": not missing,
        "required_tables": sorted(V14_7_TABLES),
        "missing_tables": missing,
        "counts": counts,
        "capabilities": [
            "proactive_timing_decision",
            "loop_interrupt_alerts",
            "social_aftereffect_alerts",
            "positive_action_windows",
            "decision_guardrails",
            "open_loop_next_steps",
            "intervention_queue",
            "cooldown_duplicate_suppression",
            "user_feedback_learning_hooks",
            "intervention_inbox_export",
            "flow_watch_autonomy",
        ],
        "commands": [
            "mlomega-audio v14-7-audit",
            "mlomega-audio v14-proactive-run [conversation_id]",
            "mlomega-audio v14-interventions",
            "mlomega-audio v14-intervention-feedback <queue_id> --type acted|dismissed|helpful|not_relevant|too_intrusive",
            "mlomega-audio v14-intervention-export",
        ],
        "limits": [
            "does not push phone notifications by itself; it creates a queue/export for a host bridge",
            "does not force action; suggestions remain hypotheses with evidence and feedback",
        ],
    }

# V18 remediation: owner-bound feedback, terminal cooldown suppression and no
# automatic resurrection of dismissed/acted interventions.
from .v18_interactions import install_interventions as _install_v18_interventions
_globals_v18_interventions = _install_v18_interventions(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_interventions)
