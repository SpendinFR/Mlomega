from __future__ import annotations

"""V14.1 Brain 2.0 Natural Router + Selection/Ranking Engine.

This layer preserves the three necessary brains instead of letting the Pattern
Mirror replace everything:

1. Raw factual recall: direct access to dated evidence, turns, source spans,
   conversations and episodes.
2. V13 prediction/simulation: predictions, similar cases, outcomes,
   relationship models and intervention branches.
3. V14 long-horizon mirror: hidden loops, blindspots, weekly/monthly snapshots,
   repetition chains and trajectory forecasts.

Important design choice requested by the owner: this module contains no regex
routing. Natural interpretation is Qwen JSON-contract based; selection/ranking
uses structured table types, timestamps, object links and stored confidence.
"""

from datetime import datetime, timezone
from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, sha256_bytes, stable_id
from .v18_legacy_forecasts import active_legacy_forecasts as _active_v14_forecasts
from .pattern_mirror_v14 import ensure_v14_schema, pattern_mirror_digest


def _safe_json(value: Any, default: Any = None) -> Any:
    """Parse persisted JSON without allowing malformed legacy payloads to crash routing."""
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json_loads(value, default)
    except Exception:
        return default

V14_1_VERSION = "14.1.0-brain2-router-selection-final"

V14_1_TABLES = {
    "v14_1_router_runs",
    "v14_1_selection_runs",
    "v14_1_selection_candidates",
    "v14_1_answer_packets",
    "v14_1_raw_recall_windows",
    "v14_1_route_contract_checks",
}

ROUTER_SCHEMA: dict[str, Any] = {
    "route_type": "raw_recall|prediction|pattern_mirror|mixed|relationship|choice|emotion|language|future_forecast|unknown",
    "question_rewrite": "",
    "needs_raw_recall": False,
    "needs_prediction_engine": False,
    "needs_pattern_mirror": False,
    "needs_relationship_model": False,
    "needs_language_model": False,
    "needs_periodic_snapshots": False,
    "time_filters": [
        {"label": "", "start_iso": None, "end_iso": None, "importance": "low|medium|high"}
    ],
    "people": [
        {"person_id_or_name": "", "role_in_question": "self|other|unknown", "importance": "low|medium|high"}
    ],
    "topics": [""],
    "prediction_targets": [
        "next_word|next_phrase|next_message|next_emotion|next_thought|next_action|next_choice|next_reaction|next_outcome|next_loop|next_risk|next_relationship_move|next_project_move|next_life_event|next_trajectory"
    ],
    "evidence_strategy": [
        "raw_turns|source_spans|episodes|states|intentions|outcomes|choices|relationships|predictions|v14_cards|v14_snapshots|language_patterns"
    ],
    "answer_goal": "",
    "missing_route_context": [],
    "confidence": 0.0,
}

ANSWER_SCHEMA: dict[str, Any] = {
    "answer": "",
    "route_type": "raw_recall|prediction|pattern_mirror|mixed|relationship|choice|emotion|language|future_forecast|unknown",
    "direct_facts": [],
    "inferences": [],
    "predictions": [],
    "pattern_mirror": [],
    "evidence": [
        {"table": "", "id": "", "quote_or_summary": "", "time": None, "confidence": 0.0}
    ],
    "counter_evidence": [],
    "confidence": 0.0,
    "what_is_fact_vs_inference": "",
    "what_is_missing": [],
    "what_to_watch_next": [],
    "intervention": "",
}

SELECTION_AUDIT_SCHEMA: dict[str, Any] = {
    "selected_items": [
        {"candidate_id": "", "reason": "", "priority": "low|medium|high|critical", "confidence": 0.0}
    ],
    "missing_context": [],
    "risk_of_missing_something": "low|medium|high",
}


def _hash_payload(payload: Any) -> str:
    return sha256_bytes(json_dumps(payload).encode("utf-8"))


def _clamp(v: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        f = float(v)
    except Exception:
        f = 0.0
    return max(lo, min(hi, f))


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _llm_json(system: str, payload: dict[str, Any], schema: dict[str, Any], timeout: int = 360) -> dict[str, Any]:
    data = OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=timeout)
    if not isinstance(data, dict):
        raise RuntimeError("Brain2 V14.1 returned non-object JSON")
    return data


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
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1").fetchone()
    return row["person_id"] if row else "me"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # fromisoformat is not regex; it accepts the ISO strings Qwen is asked to emit.
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _seconds_between(a: Any, b: Any) -> float | None:
    da = _parse_time(a)
    db = _parse_time(b)
    if da is None or db is None:
        return None
    if da.tzinfo is None:
        da = da.replace(tzinfo=timezone.utc)
    if db.tzinfo is None:
        db = db.replace(tzinfo=timezone.utc)
    return abs((da - db).total_seconds())


def _base_score(source_kind: str) -> float:
    weights = {
        "raw_turn": 0.95,
        "source_span": 0.95,
        "conversation": 0.85,
        "episode": 0.82,
        "subtopic": 0.78,
        "state": 0.72,
        "thought": 0.68,
        "speech_act": 0.70,
        "intention": 0.80,
        "outcome": 0.90,
        "choice": 0.82,
        "relationship": 0.76,
        "prediction": 0.74,
        "prediction_result": 0.88,
        "similar_case": 0.72,
        "pattern": 0.76,
        "loop": 0.80,
        "v13_insight": 0.70,
        "v14_card": 0.82,
        "v14_snapshot": 0.78,
        "v14_thread": 0.80,
        "v14_forecast": 0.78,
        "v14_chain": 0.82,
        "language": 0.86,
        "proactive_intervention": 0.90,
        "intervention_opportunity": 0.86,
    }
    return weights.get(source_kind, 0.50)


def _candidate_id(source_kind: str, row: dict[str, Any]) -> str:
    for key in [
        "turn_id", "source_span_id", "span_id", "conversation_id", "episode_id", "segment_id",
        "state_id", "thought_id", "speech_act_id", "intention_id", "outcome_id",
        "choice_id", "relationship_id", "prediction_id", "result_id", "case_id",
        "pattern_id", "loop_id", "insight_id", "card_id", "snapshot_id", "thread_id",
        "forecast_id", "chain_id", "expression_id", "ngram_id", "template_id",
    ]:
        if row.get(key):
            return f"{source_kind}:{row[key]}"
    return stable_id("v141cand", source_kind, _hash_payload(row))


def _candidate_time(row: dict[str, Any]) -> str | None:
    for key in ["started_at", "start_time", "created_at", "updated_at", "first_seen", "last_seen", "verified_at"]:
        if row.get(key):
            return str(row.get(key))
    return None


def _store_candidate(con, *, run_id: str, person_id: str, source_kind: str, table_name: str, row: dict[str, Any], reason: str, score: float) -> dict[str, Any]:
    now = now_iso()
    cid = _candidate_id(source_kind, row)
    target_id = cid.split(":", 1)[1] if ":" in cid else cid
    candidate = {
        "candidate_id": stable_id("v141storedcand", run_id, cid),
        "run_id": run_id,
        "person_id": person_id,
        "source_kind": source_kind,
        "source_table": table_name,
        "source_id": target_id,
        "event_time": _candidate_time(row),
        "score": _clamp(score),
        "selection_reason": reason,
        "payload_json": json_dumps(row),
        "created_at": now,
    }
    upsert(con, "v14_1_selection_candidates", candidate, "candidate_id")
    return candidate


def ensure_v14_1_schema() -> None:
    ensure_v14_schema()
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_1_router_runs(
                route_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                question TEXT NOT NULL,
                route_type TEXT NOT NULL,
                route_json TEXT NOT NULL,
                status TEXT NOT NULL,
                error_text TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_1_selection_runs(
                selection_run_id TEXT PRIMARY KEY,
                route_id TEXT,
                person_id TEXT NOT NULL,
                question TEXT NOT NULL,
                route_type TEXT NOT NULL,
                candidate_count INTEGER DEFAULT 0,
                top_score REAL DEFAULT 0.0,
                qwen_selection_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_1_selection_candidates(
                candidate_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_id TEXT NOT NULL,
                event_time TEXT,
                score REAL DEFAULT 0.0,
                selection_reason TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_1_answer_packets(
                answer_id TEXT PRIMARY KEY,
                route_id TEXT,
                selection_run_id TEXT,
                person_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_1_raw_recall_windows(
                window_id TEXT PRIMARY KEY,
                route_id TEXT,
                person_id TEXT NOT NULL,
                label TEXT,
                start_iso TEXT,
                end_iso TEXT,
                reason TEXT,
                candidate_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_1_route_contract_checks(
                check_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v141_candidates_person_score ON v14_1_selection_candidates(person_id, score, created_at);
            CREATE INDEX IF NOT EXISTS idx_v141_candidates_source ON v14_1_selection_candidates(source_table, source_id);
            CREATE INDEX IF NOT EXISTS idx_v141_answer_person ON v14_1_answer_packets(person_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_v141_route_person ON v14_1_router_runs(person_id, created_at);
            """
        )
        now = now_iso()
        for name in sorted(V14_1_TABLES):
            upsert(con, "v14_1_route_contract_checks", {
                "check_id": stable_id("v141check", name),
                "check_name": f"table:{name}",
                "status": "declared",
                "detail": "V14.1 natural router/selection table required. No regex routing in this layer.",
                "created_at": now,
            }, "check_id")
        con.commit()


def route_question(question: str, *, person_id: str | None = None) -> dict[str, Any]:
    ensure_v14_1_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        payload = {
            "mission": "Route la question vers les bonnes sources Brain 2.0. N'utilise pas de mots-clés simples: interprète le besoin réel. Si la question demande un fait daté, route vers raw_recall. Si elle demande un futur/probabilité, route vers prediction. Si elle demande une boucle/miroir long terme, route vers pattern_mirror. Si elle mélange tout, route mixed.",
            "question": question,
            "person_id": person_id,
            "known_source_layers": [
                "raw_turns", "source_spans", "conversations", "episodes", "states", "thoughts", "speech_acts", "intentions", "outcomes", "choices", "relationships", "interpersonal_state_mirror", "other_person_models", "emotional_couplings", "social_aftereffects", "predictions", "prediction_results", "similar_cases", "v14_cards", "v14_snapshots", "v14_threads", "language_patterns"
            ],
            "strict_output": ROUTER_SCHEMA,
        }
        try:
            route = _llm_json("Tu es le routeur naturel Brain2 V14.1. Réponds uniquement en JSON valide.", payload, ROUTER_SCHEMA, timeout=180)
            status = "ok"
            error = None
        except Exception as exc:
            route = {"route_type": "unknown", "question_rewrite": question, "missing_route_context": [str(exc)], "confidence": 0.0}
            status = "error"
            error = str(exc)
        route_id = stable_id("v141route", person_id, question, _hash_payload(route), now)
        upsert(con, "v14_1_router_runs", {
            "route_id": route_id,
            "person_id": person_id,
            "question": question,
            "route_type": str(route.get("route_type") or "unknown"),
            "route_json": json_dumps(route),
            "status": status,
            "error_text": error,
            "created_at": now,
        }, "route_id")
        con.commit()
    return {"version": V14_1_VERSION, "route_id": route_id, "person_id": person_id, "question": question, "route": route, "status": status}


def _time_windows(route: dict[str, Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for item in _as_list(route.get("time_filters")):
        if not isinstance(item, dict):
            continue
        start = item.get("start_iso")
        end = item.get("end_iso")
        if start or end:
            windows.append({"label": item.get("label") or "time_filter", "start": start, "end": end, "importance": item.get("importance") or "medium"})
    return windows


def _has_source(route: dict[str, Any], name: str) -> bool:
    return name in {str(x) for x in _as_list(route.get("evidence_strategy"))}


def _route_wants(route: dict[str, Any], key: str) -> bool:
    if bool(route.get(key)):
        return True
    route_type = str(route.get("route_type") or "unknown")
    return route_type == key.replace("needs_", "").replace("_engine", "")


def _store_rows(con, *, run_id: str, person_id: str, source_kind: str, table_name: str, rows: list[dict[str, Any]], reason: str, route_type: str, score_boost: float = 0.0) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        conf = row.get("confidence") or row.get("probability") or row.get("score") or row.get("quality_score") or 0.5
        score = _base_score(source_kind) + score_boost + 0.12 * _clamp(conf)
        if route_type == "raw_recall" and source_kind in {"raw_turn", "source_span", "conversation", "episode"}:
            score += 0.08
        if route_type in {"prediction", "future_forecast"} and source_kind in {"prediction", "similar_case", "outcome", "choice", "relationship", "v14_forecast"}:
            score += 0.08
        if route_type == "pattern_mirror" and source_kind in {"v14_card", "v14_snapshot", "v14_thread", "v14_chain"}:
            score += 0.08
        out.append(_store_candidate(con, run_id=run_id, person_id=person_id, source_kind=source_kind, table_name=table_name, row=row, reason=reason, score=score))
    return out


def _select_raw_recall(con, *, route_id: str, run_id: str, person_id: str, route: dict[str, Any], question: str, limit: int) -> list[dict[str, Any]]:
    route_type = str(route.get("route_type") or "unknown")
    candidates: list[dict[str, Any]] = []
    windows = _time_windows(route)
    now = now_iso()
    if windows:
        for w in windows:
            start = w.get("start")
            end = w.get("end")
            label = str(w.get("label") or "time_filter")
            rows: list[dict[str, Any]] = []
            if start and end:
                rows = _many(con, "SELECT * FROM conversations WHERE started_at>=? AND started_at<=? ORDER BY started_at LIMIT ?", (start, end, limit))
            elif start:
                rows = _many(con, "SELECT * FROM conversations WHERE started_at>=? ORDER BY started_at LIMIT ?", (start, limit))
            elif end:
                rows = _many(con, "SELECT * FROM conversations WHERE started_at<=? ORDER BY started_at DESC LIMIT ?", (end, limit))
            conv_ids = [r.get("conversation_id") for r in rows if r.get("conversation_id")]
            candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="conversation", table_name="conversations", rows=rows, reason=f"Raw recall window: {label}", route_type=route_type, score_boost=0.05)
            for conv_id in conv_ids[:max(1, limit // 4)]:
                turns = _many(con, "SELECT * FROM turns WHERE conversation_id=? ORDER BY idx LIMIT ?", (conv_id, 80))
                candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="raw_turn", table_name="turns", rows=turns, reason=f"Turns inside raw recall window: {label}", route_type=route_type, score_boost=0.04)
                spans = _many(con, "SELECT * FROM source_spans WHERE conversation_id=? ORDER BY start_s LIMIT ?", (conv_id, 80))
                candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="source_span", table_name="source_spans", rows=spans, reason=f"Source spans inside raw recall window: {label}", route_type=route_type, score_boost=0.04)
                episodes = _many(con, "SELECT * FROM episodes WHERE source_conversation_id=? ORDER BY start_time, created_at LIMIT ?", (conv_id, 40))
                candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="episode", table_name="episodes", rows=episodes, reason=f"Episodes inside raw recall window: {label}", route_type=route_type, score_boost=0.03)
            upsert(con, "v14_1_raw_recall_windows", {
                "window_id": stable_id("v141window", route_id, person_id, label, start or "", end or ""),
                "route_id": route_id,
                "person_id": person_id,
                "label": label,
                "start_iso": start,
                "end_iso": end,
                "reason": "Qwen routed this question to a raw temporal window.",
                "candidate_count": len(candidates),
                "created_at": now,
            }, "window_id")
    elif route.get("needs_raw_recall") or _has_source(route, "raw_turns") or route_type == "raw_recall":
        rows = _many(con, "SELECT * FROM conversations ORDER BY started_at DESC, created_at DESC LIMIT ?", (limit,))
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="conversation", table_name="conversations", rows=rows, reason="Raw recall requested without a parsed time window; taking recent conversations.", route_type=route_type)
        turns = _many(con, "SELECT * FROM turns ORDER BY conversation_id, idx LIMIT ?", (limit * 2,))
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="raw_turn", table_name="turns", rows=turns, reason="Raw recall requested; recent turns are candidate evidence.", route_type=route_type)
    return candidates


def _select_model_candidates(con, *, run_id: str, person_id: str, route: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    route_type = str(route.get("route_type") or "unknown")
    candidates: list[dict[str, Any]] = []
    wants_prediction = bool(route.get("needs_prediction_engine")) or route_type in {"prediction", "future_forecast", "mixed", "choice", "relationship"}
    wants_mirror = bool(route.get("needs_pattern_mirror")) or route_type in {"pattern_mirror", "mixed", "future_forecast"}
    wants_language = bool(route.get("needs_language_model")) or route_type == "language"
    wants_relationship = bool(route.get("needs_relationship_model")) or route_type in {"relationship", "prediction", "mixed"}

    if wants_prediction:
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="prediction", table_name="predictions", rows=_many(con, "SELECT * FROM predictions WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), reason="Prediction engine route: existing predictions and open forecasts.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="prediction_result", table_name="prediction_results", rows=_many(con, "SELECT pr.* FROM prediction_results pr JOIN predictions p ON p.prediction_id=pr.prediction_id WHERE p.person_id=? ORDER BY pr.verified_at DESC LIMIT ?", (person_id, limit)), reason="Prediction engine route: verified results calibrate future answers.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="similar_case", table_name="similar_case_scores", rows=_many(con, "SELECT * FROM similar_case_scores WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id,limit)), reason="Prediction engine route: similar cases.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="intention", table_name="action_intentions", rows=_many(con, "SELECT * FROM action_intentions WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), reason="Prediction route: open intentions/actions to watch.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="outcome", table_name="action_outcomes", rows=_many(con, "SELECT * FROM action_outcomes WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), reason="Prediction route: outcomes reveal what actually happened.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="choice", table_name="choice_episodes", rows=_many(con, "SELECT * FROM choice_episodes WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), reason="Prediction route: choice history.", route_type=route_type)
    if wants_relationship:
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="relationship", table_name="relationship_models", rows=_many(con, "SELECT * FROM relationship_models WHERE person_a=? OR person_b=? ORDER BY updated_at DESC LIMIT ?", (person_id, person_id, limit)), reason="Relationship route: relationship models and triggers.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="relationship", table_name="interaction_episodes", rows=_many(con, "SELECT * FROM interaction_episodes WHERE user_person_id=? OR other_person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, person_id, limit)), reason="Relationship route: concrete interactions.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="interpersonal_state", table_name="v14_6_other_person_state_snapshots", rows=_many(con, "SELECT * FROM v14_6_other_person_state_snapshots WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="V14.6 route: other-person moment states and emotional clues.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="emotional_coupling", table_name="v14_6_interpersonal_emotional_couplings", rows=_many(con, "SELECT * FROM v14_6_interpersonal_emotional_couplings WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="V14.6 route: how other states affect the user and vice versa.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="social_aftereffect", table_name="v14_6_social_aftereffects", rows=_many(con, "SELECT * FROM v14_6_social_aftereffects WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="V14.6 route: social aftereffects on next actions/day/mood.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="relationship_state_model", table_name="v14_6_relationship_state_models", rows=_many(con, "SELECT * FROM v14_6_relationship_state_models WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="V14.6 route: evolving models of other people and relationship dynamics.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="interpersonal_loop", table_name="v14_6_interpersonal_loop_cards", rows=_many(con, "SELECT * FROM v14_6_interpersonal_loop_cards WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="V14.6 route: repeated interpersonal loops and escape conditions.", route_type=route_type)
    if wants_mirror:
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="v14_card", table_name="v14_pattern_mirror_cards", rows=_many(con, "SELECT * FROM v14_pattern_mirror_cards WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="Pattern mirror route: hidden pattern cards.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="v14_snapshot", table_name="v14_periodic_self_snapshots", rows=_many(con, "SELECT * FROM v14_periodic_self_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), reason="Pattern mirror route: periodic self snapshots.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="v14_thread", table_name="v14_long_horizon_threads", rows=_many(con, "SELECT * FROM v14_long_horizon_threads WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="Pattern mirror route: long-horizon threads.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="v14_forecast", table_name="v14_trajectory_forecasts", rows=_active_v14_forecasts(con, person_id=person_id, source_table="v14_trajectory_forecasts", limit=limit), reason="Pattern mirror route: V18 lifecycle-gated trajectory forecasts.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="v14_chain", table_name="v14_repetition_chains", rows=_many(con, "SELECT * FROM v14_repetition_chains WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="Pattern mirror route: repetition chains.", route_type=route_type)
    if wants_language:
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="language", table_name="personal_language_patterns", rows=_many(con, "SELECT * FROM personal_language_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="Language route: personal language patterns.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="language", table_name="language_ngrams", rows=_many(con, "SELECT * FROM language_ngrams WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="Language route: n-grams and new verbal tics.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="language", table_name="phrase_templates", rows=_many(con, "SELECT * FROM phrase_templates WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="Language route: phrase templates.", route_type=route_type)
    if route_type in {"emotion", "mixed", "prediction", "pattern_mirror", "future_forecast", "choice", "relationship"}:
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="proactive_intervention", table_name="v14_7_intervention_queue", rows=_many(con, "SELECT * FROM v14_7_intervention_queue WHERE person_id=? AND status IN ('ready','pending','snoozed') ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC LIMIT ?", (person_id, limit)), reason="V14.7 route: timing-aware proactive intervention inbox.", route_type=route_type, score_boost=0.08)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="intervention_opportunity", table_name="v14_7_intervention_opportunities", rows=_many(con, "SELECT * FROM v14_7_intervention_opportunities WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), reason="V14.7 route: proactive intervention opportunities and why-now logic.", route_type=route_type, score_boost=0.05)

    if route_type in {"emotion", "mixed", "prediction", "pattern_mirror"}:
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="state", table_name="internal_state_snapshots", rows=_many(con, "SELECT * FROM internal_state_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), reason="State/emotion route: internal state snapshots.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="thought", table_name="thought_hypotheses", rows=_many(con, "SELECT * FROM thought_hypotheses WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), reason="Thought/emotion route: thought hypotheses.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="emotional_coupling", table_name="v14_6_interpersonal_emotional_couplings", rows=_many(con, "SELECT * FROM v14_6_interpersonal_emotional_couplings WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="V14.6 emotion route: interpersonal emotional coupling.", route_type=route_type)
        candidates += _store_rows(con, run_id=run_id, person_id=person_id, source_kind="social_aftereffect", table_name="v14_6_social_aftereffects", rows=_many(con, "SELECT * FROM v14_6_social_aftereffects WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), reason="V14.6 emotion route: social aftereffects.", route_type=route_type)
    return candidates


def select_candidates(question: str, *, person_id: str | None = None, route_payload: dict[str, Any] | None = None, limit: int = 80) -> dict[str, Any]:
    ensure_v14_1_schema()
    route_info = route_payload or route_question(question, person_id=person_id)
    route = route_info.get("route") or route_info
    route_id = str(route_info.get("route_id") or stable_id("v141route_ext", question, _hash_payload(route)))
    now = now_iso()
    with connect() as con:
        person_id = person_id or str(route_info.get("person_id") or _default_user(con))
        run_id = stable_id("v141selection", person_id, question, route_id, now)
        candidates: list[dict[str, Any]] = []
        candidates += _select_raw_recall(con, route_id=route_id, run_id=run_id, person_id=person_id, route=route, question=question, limit=limit)
        candidates += _select_model_candidates(con, run_id=run_id, person_id=person_id, route=route, limit=limit)
        stored = _many(con, "SELECT * FROM v14_1_selection_candidates WHERE run_id=? ORDER BY score DESC, created_at DESC LIMIT ?", (run_id, limit * 3))
        # Qwen does the final cognitive selection among candidates. If Qwen fails, the
        # deterministic ranking still preserves table/time/evidence ordering but does
        # not invent psychological meaning.
        try:
            audit = _llm_json(
                "Tu es le sélectionneur Brain2 V14.1. Choisis seulement parmi les candidates fournies. Réponds en JSON.",
                {
                    "question": question,
                    "route": route,
                    "candidates": stored[:limit],
                    "instruction": "Sélectionne les éléments qui doivent remonter. Pas de psychologie générique. Chaque sélection doit expliquer pourquoi cette preuve est utile.",
                    "schema": SELECTION_AUDIT_SCHEMA,
                },
                SELECTION_AUDIT_SCHEMA,
                timeout=240,
            )
        except Exception as exc:
            audit = {"selected_items": [], "missing_context": [str(exc)], "risk_of_missing_something": "medium"}
        top_score = float(stored[0]["score"]) if stored else 0.0
        upsert(con, "v14_1_selection_runs", {
            "selection_run_id": run_id,
            "route_id": route_id,
            "person_id": person_id,
            "question": question,
            "route_type": str(route.get("route_type") or "unknown"),
            "candidate_count": len(stored),
            "top_score": top_score,
            "qwen_selection_json": json_dumps(audit),
            "created_at": now,
        }, "selection_run_id")
        con.commit()
    return {"version": V14_1_VERSION, "selection_run_id": run_id, "route_id": route_id, "person_id": person_id, "question": question, "route": route, "candidate_count": len(stored), "candidates": stored[:limit], "qwen_selection": audit}


def ask_brain2(question: str, *, person_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    """Natural interface over raw recall + V13 prediction + V14 mirror.

    The user does not choose next_* manually. Qwen routes the question, the
    selector retrieves the right evidence layer, and the final answer must state
    what is fact, what is inference, what is prediction and what is missing.
    """
    ensure_v14_1_schema()
    route_info = route_question(question, person_id=person_id)
    if route_info.get("status") != "ok":
        return {"version": V14_1_VERSION, "status": "route_failed", **route_info}
    selected = select_candidates(question, person_id=route_info.get("person_id"), route_payload=route_info, limit=limit)
    now = now_iso()
    with connect() as con:
        person_id = str(route_info.get("person_id") or _default_user(con))
        payload = {
            "mission": "Réponds au nom du cerveau 2.0 complet. Utilise la bonne couche: brut pour faits datés, V13 pour prédictions/simulations, V14 pour boucles longues. Ne transforme jamais une consolidation V14 en preuve brute. Sépare toujours fait, inférence, prédiction et manque de contexte.",
            "question": question,
            "route": selected.get("route"),
            "selected_candidates": selected.get("candidates", [])[:limit],
            "qwen_selection": selected.get("qwen_selection"),
            "v14_digest": pattern_mirror_digest(person_id=person_id, limit=30),
            "answer_schema": ANSWER_SCHEMA,
        }
        out = _llm_json("Tu es l'interface Brain2 V14.1. Réponds uniquement en JSON valide.", payload, ANSWER_SCHEMA, timeout=360)
        answer_id = stable_id("v141answer", person_id, question, selected.get("selection_run_id"), _hash_payload(out), now)
        upsert(con, "v14_1_answer_packets", {
            "answer_id": answer_id,
            "route_id": selected.get("route_id"),
            "selection_run_id": selected.get("selection_run_id"),
            "person_id": person_id,
            "question": question,
            "answer_json": json_dumps(out),
            "created_at": now,
        }, "answer_id")
        con.commit()
    return {"version": V14_1_VERSION, "answer_id": answer_id, "person_id": person_id, "question": question, "route": selected.get("route"), "selection": {"candidate_count": selected.get("candidate_count"), "selection_run_id": selected.get("selection_run_id")}, **out}


def audit_v14_1(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_1_schema()
    with connect() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(V14_1_TABLES - tables)
        counts: dict[str, Any] = {}
        for t in sorted(V14_1_TABLES):
            counts[t] = _one_value(con, f"SELECT COUNT(*) AS c FROM {t}", default="missing")
        if persist:
            now = now_iso()
            for t in sorted(V14_1_TABLES):
                upsert(con, "v14_1_route_contract_checks", {
                    "check_id": stable_id("v141audit", t),
                    "check_name": f"exists:{t}",
                    "status": "ok" if t in tables else "missing",
                    "detail": "V14.1 route/selection/raw recall/prediction/mirror access check.",
                    "created_at": now,
                }, "check_id")
            con.commit()
    return {
        "version": V14_1_VERSION,
        "ok": not missing,
        "missing_tables": missing,
        "required_tables": sorted(V14_1_TABLES),
        "counts": counts,
        "routing_layers": {
            "raw_recall": "for dated/place/factual questions; uses raw conversations/turns/source_spans/episodes",
            "prediction": "for what will happen/do/say/feel/choose; uses V13 predictions/similar cases/outcomes/relationships",
            "pattern_mirror": "for loops/blindspots/long-horizon self model; uses V14 cards/snapshots/threads/chains",
            "mixed": "uses all layers and keeps fact vs inference vs prediction separated",
        },
        "no_regex_layer": "This V14.1 module does not import or use regex. Qwen routes natural language; selection uses structured tables and timestamps.",
    }

# --- V18 scope hardening ---------------------------------------------------
# Keep the public V14 API but remove implicit-owner and cross-conversation
# retrieval.  These overrides deliberately run after the legacy definitions.
from .governance_v18 import ScopeError as _V18ScopeError, conversation_in_scope as _v18_conversation_in_scope, projection_is_active as _v18_projection_is_active

_v17_route_question_v141 = route_question
_v17_select_raw_recall_v141 = _select_raw_recall
_v17_select_model_candidates_v141 = _select_model_candidates
_v17_select_candidates_v141 = select_candidates
_v17_ask_brain2_v141 = ask_brain2

def _v18_candidate_owner_ok_v141(con, candidate: dict[str, Any], person_id: str) -> bool:
    table = str(candidate.get("source_table") or "")
    sid = str(candidate.get("source_id") or "")
    if not table or not sid:
        return False
    # Explicit V18 projection retirement overrides every old table status.
    try:
        if not _v18_projection_is_active(con, projection_kind="router_source", source_table=table, source_id=sid, person_id=person_id):
            return False
    except Exception:
        return False
    if table == "conversations":
        return _v18_conversation_in_scope(con, conversation_id=sid, person_id=person_id)
    if table in {"turns", "source_spans", "episodes", "conversation_subtopic_segments"}:
        col = {"turns":"conversation_id","source_spans":"conversation_id","episodes":"source_conversation_id","conversation_subtopic_segments":"conversation_id"}[table]
        pk = {"turns":"turn_id","source_spans":"span_id","episodes":"episode_id","conversation_subtopic_segments":"subtopic_id"}[table]
        row = con.execute(f"SELECT {col} AS conversation_id FROM {table} WHERE {pk}=?", (sid,)).fetchone()
        return bool(row and row["conversation_id"] and _v18_conversation_in_scope(con, conversation_id=str(row["conversation_id"]), person_id=person_id))
    # Most model tables declare person_id.  Introspect rather than assuming.
    cols = {str(r["name"]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if "person_id" in cols:
        pk_candidates = ["prediction_id", "case_id", "similar_case_id", "candidate_pattern_id", "confirmed_pattern_id", "pattern_id", "card_id", "snapshot_id", "queue_id", "opportunity_id", "state_id", "thought_id", "intention_id", "outcome_id", "choice_id", "language_pattern_id", "ngram_id", "template_id"]
        pk = next((x for x in pk_candidates if x in cols), None)
        if not pk:
            # Candidate payload must carry a person id if a safe primary key is
            # unknown.  Reject rather than issuing an unbounded table read.
            payload = _safe_json(candidate.get("payload_json"), {}) or {}
            return str(payload.get("person_id") or "") == person_id
        row = con.execute(f"SELECT person_id FROM {table} WHERE {pk}=?", (sid,)).fetchone()
        return bool(row and str(row["person_id"] or "") == person_id)
    if table == "relationship_models":
        row = con.execute("SELECT 1 FROM relationship_models WHERE relationship_id=? AND (person_a=? OR person_b=?)", (sid, person_id, person_id)).fetchone()
        return bool(row)
    return False


def route_question(question: str, *, person_id: str | None = None) -> dict[str, Any]:
    if not person_id:
        raise _V18ScopeError("V18 Brain2 routing requires an explicit person_id")
    return _v17_route_question_v141(question, person_id=person_id)


def _select_raw_recall(con, *, route_id: str, run_id: str, person_id: str, route: dict[str, Any], question: str, limit: int) -> list[dict[str, Any]]:
    """Scoped raw recall: raw turns are only reachable through an owner proof."""
    route_type = str(route.get("route_type") or "unknown")
    candidates: list[dict[str, Any]] = []
    windows = _time_windows(route) or [{"label":"recent_scoped","start":None,"end":None}]
    for w in windows:
        start,end,label = w.get("start"),w.get("end"),str(w.get("label") or "scoped")
        clauses=["cs.person_id=?","cs.active=1"]; params: list[Any]=[person_id]
        if start: clauses.append("c.started_at>=?"); params.append(start)
        if end: clauses.append("c.started_at<=?"); params.append(end)
        rows=_many(con, f"SELECT c.* FROM conversations c JOIN v18_conversation_scopes cs ON cs.conversation_id=c.conversation_id WHERE {' AND '.join(clauses)} ORDER BY c.started_at DESC,c.created_at DESC LIMIT ?", tuple(params+[limit]))
        candidates += _store_rows(con,run_id=run_id,person_id=person_id,source_kind="conversation",table_name="conversations",rows=rows,reason=f"Scoped raw recall: {label}",route_type=route_type,score_boost=.05)
        for row in rows[:max(1,limit//4)]:
            cid=str(row["conversation_id"])
            candidates += _store_rows(con,run_id=run_id,person_id=person_id,source_kind="raw_turn",table_name="turns",rows=_many(con,"SELECT * FROM turns WHERE conversation_id=? ORDER BY idx LIMIT ?",(cid,80)),reason="Scoped turns",route_type=route_type,score_boost=.04)
            candidates += _store_rows(con,run_id=run_id,person_id=person_id,source_kind="source_span",table_name="source_spans",rows=_many(con,"SELECT * FROM source_spans WHERE conversation_id=? ORDER BY start_s LIMIT ?",(cid,80)),reason="Scoped spans",route_type=route_type,score_boost=.04)
            candidates += _store_rows(con,run_id=run_id,person_id=person_id,source_kind="episode",table_name="episodes",rows=_many(con,"SELECT * FROM episodes WHERE source_conversation_id=? AND COALESCE(lifecycle_status,'active') NOT IN ('obsolete','invalidated','deleted','contradicted') ORDER BY start_time,created_at LIMIT ?",(cid,40)),reason="Scoped active episodes",route_type=route_type,score_boost=.03)
    return candidates


def _select_model_candidates(con, *, run_id: str, person_id: str, route: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    # The legacy selector provides broad model coverage; V18 removes every
    # candidate that fails owner/projection validation before its prompt is made.
    candidates = _v17_select_model_candidates_v141(con, run_id=run_id, person_id=person_id, route=route, limit=limit)
    allowed=[]; rejected=[]
    for c in candidates:
        if _v18_candidate_owner_ok_v141(con,c,person_id):
            allowed.append(c)
        else:
            rejected.append(c.get("candidate_id"))
    if rejected:
        placeholders=",".join("?" for _ in rejected if _)
        if placeholders:
            con.execute(f"DELETE FROM v14_1_selection_candidates WHERE run_id=? AND candidate_id IN ({placeholders})", (run_id,*[x for x in rejected if x]))
    return allowed


def select_candidates(question: str, *, person_id: str | None = None, route_payload: dict[str, Any] | None = None, limit: int = 80) -> dict[str, Any]:
    if not person_id and not (route_payload or {}).get("person_id"):
        raise _V18ScopeError("V18 candidate selection requires an explicit person_id")
    out = _v17_select_candidates_v141(question, person_id=person_id, route_payload=route_payload, limit=limit)
    # Defense in depth for pre-existing candidates created by legacy code.
    with connect() as con:
        safe=[c for c in out.get("candidates") or [] if _v18_candidate_owner_ok_v141(con,c,str(out["person_id"]))]
    out["candidates"]=safe
    out["candidate_count"]=len(safe)
    return out


def ask_brain2(question: str, *, person_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    if not person_id:
        raise _V18ScopeError("V18 Brain2 answers require an explicit person_id")
    return _v17_ask_brain2_v141(question, person_id=person_id, limit=limit)
