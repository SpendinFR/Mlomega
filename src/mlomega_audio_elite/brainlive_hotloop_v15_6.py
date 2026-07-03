from __future__ import annotations

"""V15.6 BrainLive hot loop: identity/context readiness + trigger router + 12s budget.

This layer is the part that makes BrainLive behave like a live system instead
of a collection of model calls:

- do not re-identify the same active person on every turn;
- keep the Brain2 context hot before the important sentence arrives;
- refresh context only when person/place/situation changed or TTL expired;
- route H0/H1/H2 from model-evaluated triggers, not regex;
- use one unified LLM call for short-horizon prediction when possible so the
  live loop has a realistic chance to fit inside a 12s budget;
- store exact latency stages and readiness so failures are measurable.

No psychological meaning is inferred by regex or keywords. Signal-processing
rules are allowed only for identity/context TTL, cache invalidation, and latency
budgeting. Meaning-making is LLM/VLM/Brain2-context only.
"""

import json
import math
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .brainlive_realtime_v15_2 import resolve_active_speaker
from .brainlive_sensor_fusion_v15_4 import build_fused_situation, resolve_place_multisource, _tick_decision
from .brainlive_v15 import build_active_context, ensure_brainlive_schema, ingest_live_turn
from .config import get_settings
from .db import connect, init_db, upsert, write_transaction
from .llm import OllamaJsonClient
from .integrity_v176 import ContractValidationError, create_forecast, quarantine_in_transaction
from .v18_delivery import enqueue_delivery, ensure_delivery_schema
from .v18_hot_capsule import build_hot_capsule_payload
from .v18_runtime_hardening import (
    classify_llm_exception, claim_llm_decision_run, ensure_llm_decision_run,
    ensure_runtime_hardening_schema, finish_llm_decision_run, persist_episode_capsule,
    record_capsule_prompt_rendering, record_llm_evidence_requests, resolve_llm_evidence_requests,
    validate_manifest_evidence, validate_resolvable_manifest_evidence,
)
from .utils import json_dumps, json_loads, now_iso, stable_id

VERSION = "15.6.0-hot-loop-budgeted"

HOT_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_hot_identity_cache(
  cache_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  speaker_slot TEXT NOT NULL DEFAULT 'primary',
  person_id TEXT,
  label TEXT,
  identity_status TEXT NOT NULL,
  confidence REAL DEFAULT 0.0,
  source TEXT,
  evidence_json TEXT DEFAULT '{}',
  first_seen_at TEXT NOT NULL,
  last_verified_at TEXT NOT NULL,
  expires_at_epoch REAL,
  turn_count_since_verify INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_hot_context_cache(
  cache_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  active_people_json TEXT DEFAULT '[]',
  place_json TEXT DEFAULT '{}',
  active_context_id TEXT,
  active_context_digest TEXT,
  ready_horizons_json TEXT DEFAULT '{}',
  refresh_reason_json TEXT DEFAULT '{}',
  context_payload_json TEXT DEFAULT '{}',
  built_latency_ms INTEGER,
  expires_at_epoch REAL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_hot_trigger_routes(
  route_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  fused_id TEXT,
  route_status TEXT NOT NULL,
  triggered_horizons_json TEXT DEFAULT '[]',
  router_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '{}',
  latency_ms INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_hot_budget_runs(
  budget_run_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  fused_id TEXT,
  route_id TEXT,
  target_total_ms INTEGER DEFAULT 12000,
  observed_total_ms INTEGER,
  stages_json TEXT DEFAULT '{}',
  latency_ok INTEGER DEFAULT 0,
  status TEXT NOT NULL,
  result_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_hot_intervention_log(
  hot_intervention_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  budget_run_id TEXT,
  horizon TEXT,
  decision TEXT NOT NULL,
  message TEXT,
  expected_gain REAL DEFAULT 0.0,
  confidence REAL DEFAULT 0.0,
  reason_json TEXT DEFAULT '{}',
  delivery_status TEXT DEFAULT 'queued',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blhot_ident_session ON brainlive_hot_identity_cache(live_session_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_blhot_ctx_session ON brainlive_hot_context_cache(live_session_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_blhot_budget_session ON brainlive_hot_budget_runs(live_session_id, created_at);
"""

HOT_UNIFIED_SCHEMA: dict[str, Any] = {
    "world_state": {
        "where_am_i": "",
        "who_is_active": [],
        "what_is_happening": "",
        "probable_activity": [],
        "active_mode": "",
        "confidence": 0.0,
        "evidence": [],
        "counter_evidence": [],
        "missing_evidence": [{"source_table": "", "source_id": ""}],
    },
    "horizons": {
        "H0": {"summary": "", "needs": [], "risks_or_opportunities": [], "intervention_candidates": [], "watch_next": [], "confidence": 0.0, "evidence": [{"source_table": "", "source_id": ""}], "counter_evidence": [{"source_table": "", "source_id": ""}]},
        "H1": {"summary": "", "needs": [], "risks_or_opportunities": [], "intervention_candidates": [], "watch_next": [], "confidence": 0.0, "evidence": [{"source_table": "", "source_id": ""}], "counter_evidence": [{"source_table": "", "source_id": ""}]},
        "H2": {"summary": "", "needs": [], "risks_or_opportunities": [], "intervention_candidates": [], "watch_next": [], "confidence": 0.0, "evidence": [{"source_table": "", "source_id": ""}], "counter_evidence": [{"source_table": "", "source_id": ""}]},
    },
    "active_predictions": [
        {"prediction": "", "horizon": "H0|H1|H2", "probability": 0.0, "confidence": 0.0, "evidence": [{"source_table": "", "source_id": ""}], "counter_evidence": [{"source_table": "", "source_id": ""}], "what_would_confirm": [], "what_would_refute": []}
    ],
    "proactive_decision": {
        "decision": "observe|speak_now|queue|wait",
        "message": "",
        "horizon": "H0|H1|H2",
        "expected_gain": 0.0,
        "intrusion_cost": 0.0,
        "confidence": 0.0,
        "why_now": "",
        "risk_if_wrong": "",
        "evidence": [{"source_table": "", "source_id": ""}],
        "counter_evidence": [{"source_table": "", "source_id": ""}],
    },
    "notes_for_brain2": [],
    "uncertainties": [],
    # Optional: a bounded second pass is permitted only when the model names
    # already-announced manifest references. It is never free-form retrieval.
    "needs_evidence": None,
}

ROUTER_SCHEMA: dict[str, Any] = {
    "route_status": "observe|run_h0|run_h0_h1|run_h0_h1_h2",
    "triggered_horizons": [],
    "why": "",
    "time_sensitivity": 0.0,
    "meaningful_change": 0.0,
    "proactive_potential": 0.0,
    "confidence": 0.0,
    "evidence": [],
    "missing_evidence": [],
}


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _clamp(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(0.0, min(1.0, v))


def _text_or_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json_dumps(value)
    except Exception:
        return str(value)


def _hot_prediction_person_id(con, live_session_id: str, hot_context: dict[str, Any]) -> str:
    ctx = hot_context.get("context") if isinstance(hot_context, dict) else {}
    if isinstance(ctx, dict):
        sess = ctx.get("session") if isinstance(ctx.get("session"), dict) else {}
        if sess.get("person_id"):
            return str(sess.get("person_id"))
    row = _one(con, "SELECT person_id FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
    return str((row or {}).get("person_id") or "me")


def _materialize_active_predictions(
    con,
    *,
    live_session_id: str,
    budget_run_id: str,
    fused: dict[str, Any],
    hot_context: dict[str, Any],
    result: dict[str, Any],
    now: str,
) -> int:
    """Persist hot forecasts through the same lifecycle writer as BrainLive.

    Old code copied ``confidence`` into ``probability`` and destructive-upserted
    records.  V17.6 requires both fields and quarantines invalid hot output
    instead of fabricating a probability.
    """
    if not isinstance(result, dict):
        return 0
    predictions = result.get("active_predictions")
    if not isinstance(predictions, list):
        return 0
    person_id = _hot_prediction_person_id(con, live_session_id, hot_context)
    event_id = fused.get("event_id") or fused.get("primary_event_id")
    if not event_id and isinstance(fused.get("event"), dict):
        event_id = fused["event"].get("event_id")
    count = 0
    for ordinal, p in enumerate(predictions):
        if not isinstance(p, dict):
            quarantine_in_transaction(
                con, category="invalid_hot_prediction", reason="active prediction is not an object",
                raw_payload=p, run_id=budget_run_id, source_table="brainlive_hot_budget_runs",
                source_id=budget_run_id, person_id=person_id,
            )
            continue
        prediction_text = p.get("prediction") or p.get("predicted_action") or p.get("predicted_need") or p.get("predicted_risk") or p.get("predicted_opportunity")
        if not prediction_text:
            quarantine_in_transaction(
                con, category="invalid_hot_prediction", reason="missing prediction text",
                raw_payload=p, run_id=budget_run_id, source_table="brainlive_hot_budget_runs",
                source_id=f"{budget_run_id}:{ordinal}", person_id=person_id,
            )
            continue
        # Do not reuse confidence as a made-up event probability.
        if "probability" not in p:
            quarantine_in_transaction(
                con, category="invalid_hot_prediction", reason="probability missing; confidence is not a probability",
                raw_payload=p, run_id=budget_run_id, source_table="brainlive_hot_budget_runs",
                source_id=f"{budget_run_id}:{ordinal}", person_id=person_id,
            )
            continue
        evidence = list(p.get("evidence") or [])
        confirm = p.get("what_would_confirm") or []
        if confirm:
            evidence.append({"what_would_confirm": confirm})
        payload = {
            "horizon": str(p.get("horizon") or "H1"),
            "forecast_type": str(p.get("forecast_type") or "trajectory"),
            "predicted_need": p.get("need") or p.get("predicted_need"),
            "predicted_action": _text_or_json(prediction_text),
            "predicted_words": _text_or_json(p.get("predicted_words")),
            "predicted_emotion": _text_or_json(p.get("predicted_emotion")),
            "predicted_risk": _text_or_json(p.get("risk") or p.get("predicted_risk")),
            "predicted_opportunity": _text_or_json(p.get("opportunity") or p.get("predicted_opportunity")),
            "if_intervene_future": _text_or_json(p.get("if_intervene_future")),
            "if_silent_future": _text_or_json(p.get("if_silent_future")),
            "expected_gain": p.get("expected_gain", 0.0),
            "probability": p.get("probability"),
            "confidence": p.get("confidence"),
            "evidence": evidence,
            "counter_evidence": p.get("counter_evidence") or p.get("what_would_refute") or [],
        }
        try:
            create_forecast(
                con,
                live_session_id=live_session_id,
                person_id=person_id,
                event_id=event_id,
                run_id=budget_run_id,
                payload=payload,
                occurred_at=now,
                source="hotloop",
            )
            count += 1
        except (ContractValidationError, ValueError) as exc:
            quarantine_in_transaction(
                con, category="invalid_hot_prediction", reason=str(exc)[:2000], raw_payload=p,
                run_id=budget_run_id, source_table="brainlive_hot_budget_runs",
                source_id=f"{budget_run_id}:{ordinal}", person_id=person_id,
            )
    return count


def _epoch_from_now(seconds: float) -> float:
    return time.time() + float(seconds)


def ensure_hotloop_schema() -> None:
    ensure_brainlive_schema()
    init_db()
    ensure_runtime_hardening_schema()
    ensure_delivery_schema()
    with connect() as con:
        con.executescript(HOT_SCHEMA)
        con.commit()


def _latest_identity_cache(con, live_session_id: str, speaker_slot: str = "primary") -> dict[str, Any] | None:
    return _one(con, "SELECT * FROM brainlive_hot_identity_cache WHERE live_session_id=? AND speaker_slot=? ORDER BY updated_at DESC LIMIT 1", (live_session_id, speaker_slot))


def _update_identity_cache(con, *, live_session_id: str, resolution: dict[str, Any], speaker_slot: str = "primary", ttl_s: float = 90.0) -> dict[str, Any]:
    now = now_iso()
    pid = resolution.get("person_id")
    label = resolution.get("label") or pid or "unknown_speaker"
    conf = _clamp(resolution.get("confidence"))
    source = resolution.get("source") or "unknown"
    status = "verified" if pid and conf >= 0.70 and source in {"explicit_upstream", "speechbrain_voice_match"} else ("hypothesis_unverified" if pid else "unresolved")
    row = {
        "cache_id": stable_id("blhotid", live_session_id, speaker_slot),
        "live_session_id": live_session_id,
        "speaker_slot": speaker_slot,
        "person_id": pid,
        "label": label,
        "identity_status": status,
        "confidence": conf,
        "source": source,
        "evidence_json": json_dumps(resolution),
        "first_seen_at": now,
        "last_verified_at": now,
        "expires_at_epoch": _epoch_from_now(ttl_s if conf >= 0.65 else min(20.0, ttl_s)),
        "turn_count_since_verify": 0,
        "updated_at": now,
    }
    old = _latest_identity_cache(con, live_session_id, speaker_slot)
    if old:
        row["first_seen_at"] = old.get("first_seen_at") or now
    upsert(con, "brainlive_hot_identity_cache", row, "cache_id")
    return row


def _llm_identity_hypothesis(live_session_id: str, *, speaker_label: str | None, audio_resolution: dict[str, Any], ttl_context: dict[str, Any] | None = None, timeout: float = 4.0) -> dict[str, Any]:
    """Infer a *hypothesis* only when voice matching fails.

    This is not face recognition and not a verified identity. It asks the local
    LLM to compare current context, active people and VLM/person hints with known
    Brain2 context. It may return a person_id candidate with low/medium confidence,
    always marked unverified.
    """
    try:
        client = OllamaJsonClient()
        with connect() as con:
            active_people = _many(con, "SELECT person_id,label,confidence,source,evidence_json FROM brainlive_person_presence WHERE live_session_id=? AND status='active' ORDER BY confidence DESC,last_seen_at DESC LIMIT 8", (live_session_id,)) if _table_exists(con, "brainlive_person_presence") else []
            vision = _many(con, "SELECT normalized_json,confidence,created_at FROM brainlive_vlm_observations_v154 WHERE live_session_id=? ORDER BY created_at DESC LIMIT 3", (live_session_id,)) if _table_exists(con, "brainlive_vlm_observations_v154") else []
            latest_ctx = _one(con, "SELECT context_payload_json FROM brainlive_hot_context_cache WHERE live_session_id=? ORDER BY updated_at DESC LIMIT 1", (live_session_id,))
        payload = {
            "mission": "If voice recognition failed, propose at most one identity hypothesis from active context. Never mark it verified. If evidence is weak, return null person_id.",
            "speaker_label": speaker_label,
            "voice_resolution": audio_resolution,
            "active_people": active_people,
            "recent_vision": [{**v, "normalized": json_loads(v.get("normalized_json"), {})} for v in vision],
            "hot_context_light": json_loads((latest_ctx or {}).get("context_payload_json"), {}) if latest_ctx else (ttl_context or {}),
            "rules": ["no regex", "no keyword psychology", "identity can be hypothesis only unless voice/explicit", "return uncertainty"],
        }
        out = client.require_json(
            "Tu es BrainLive Identity Fusion. Tu ne vérifies pas une identité sans preuve vocale/explicite. Tu peux proposer une hypothèse faible si contexte/vision/personnes actives le soutiennent. JSON strict.",
            json_dumps(payload),
            schema_hint={"person_id": None, "label": "", "confidence": 0.0, "why": "", "evidence": [], "missing_evidence": [], "identity_status": "hypothesis_unverified|unresolved"},
            timeout=timeout,
        )
        return {
            "person_id": out.get("person_id"),
            "label": out.get("label") or speaker_label or out.get("person_id") or "unknown_speaker",
            "confidence": min(_clamp(out.get("confidence")), 0.62),
            "source": "llm_context_identity_hypothesis",
            "identity_status": "hypothesis_unverified" if out.get("person_id") else "unresolved",
            "why": out.get("why"),
            "evidence": out.get("evidence") or [],
            "missing_evidence": out.get("missing_evidence") or [],
        }
    except Exception as exc:
        return {"person_id": None, "label": speaker_label or "unknown_speaker", "confidence": 0.0, "source": "identity_llm_error", "error": str(exc)[:800], "identity_status": "unresolved"}


def resolve_speaker_hot(
    live_session_id: str,
    *,
    audio_sample_path: str | None = None,
    explicit_person_id: str | None = None,
    speaker_label: str | None = None,
    explicit_confidence: float | None = None,
    force_verify: bool = False,
    ttl_s: float = 90.0,
) -> dict[str, Any]:
    """Resolve speaker once, then keep it hot instead of redoing recognition every turn."""
    ensure_hotloop_schema()
    with connect() as con:
        cached = _latest_identity_cache(con, live_session_id)
        if cached and not force_verify and not explicit_person_id:
            not_expired = float(cached.get("expires_at_epoch") or 0.0) > time.time()
            strong = _clamp(cached.get("confidence")) >= 0.65 and cached.get("person_id")
            if not_expired and strong:
                # Do not blindly keep forever; count turns since last verification.
                cached["turn_count_since_verify"] = int(cached.get("turn_count_since_verify") or 0) + 1
                cached["updated_at"] = now_iso()
                row_to_save = dict(cached)
                row_to_save.pop("cache_hit", None)
                upsert(con, "brainlive_hot_identity_cache", row_to_save, "cache_id")
                con.commit()
                return {"person_id": cached.get("person_id"), "label": cached.get("label"), "confidence": _clamp(cached.get("confidence")), "source": "hot_identity_cache", "identity_status": cached.get("identity_status"), "cache_hit": True}
    # Need new verification/hypothesis.
    resolution = resolve_active_speaker(audio_sample_path=audio_sample_path, explicit_person_id=explicit_person_id, speaker_label=speaker_label, explicit_confidence=explicit_confidence)
    if not resolution.get("person_id") and not explicit_person_id:
        resolution = _llm_identity_hypothesis(live_session_id, speaker_label=speaker_label, audio_resolution=resolution)
    with connect() as con:
        row = _update_identity_cache(con, live_session_id=live_session_id, resolution=resolution, ttl_s=ttl_s)
        con.commit()
    return {"person_id": row.get("person_id"), "label": row.get("label"), "confidence": _clamp(row.get("confidence")), "source": row.get("source"), "identity_status": row.get("identity_status"), "cache_hit": False, "evidence": json_loads(row.get("evidence_json"), {})}


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _stable_digest(obj: Any) -> str:
    return stable_id("digest", json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)[:4000])


def prepare_hot_context(
    live_session_id: str,
    *,
    person_id: str | None = None,
    active_people: list[str] | None = None,
    place: dict[str, Any] | None = None,
    reason: str = "cadence",
    ttl_s: float = 45.0,
    force: bool = False,
    limit: int = 32,
) -> dict[str, Any]:
    """Keep Brain2 context ready before it is needed."""
    ensure_hotloop_schema()
    active_people = [p for p in (active_people or []) if p]
    place = place or {}
    wanted_digest = _stable_digest({"people": active_people, "place": place.get("place_label") or place.get("location_hint") or place.get("label")})
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,)) or {}
        person_id = person_id or sess.get("person_id") or "me"
        cached = _one(con, "SELECT * FROM brainlive_hot_context_cache WHERE live_session_id=? ORDER BY updated_at DESC LIMIT 1", (live_session_id,))
        if cached and not force:
            same_digest = cached.get("active_context_digest") == wanted_digest
            fresh = float(cached.get("expires_at_epoch") or 0.0) > time.time()
            if same_digest and fresh and cached.get("active_context_id"):
                return {"status": "cache_hit", "active_context_id": cached.get("active_context_id"), "context": json_loads(cached.get("context_payload_json"), {}), "latency_ms": 0, "reason": "hot_context_cache"}
    t0 = time.time()
    built = build_active_context(live_session_id, active_people=active_people or None, limit=limit)
    latency_ms = int((time.time() - t0) * 1000)
    context = built.get("context", {})
    active_context_id = built.get("active_context_id")
    ready = {
        "H0": bool(active_context_id),
        "H1": bool(active_context_id and (context.get("brain2_context") or context.get("recent_turns_summary"))),
        "H2": bool(active_context_id and (context.get("brain2_context") or context.get("brainlive_routine_cards") or context.get("life_hypotheses_json"))),
    }
    now = now_iso()
    row = {
        "cache_id": stable_id("blhotctx", live_session_id),
        "live_session_id": live_session_id,
        "person_id": str(person_id),
        "active_people_json": json_dumps(active_people),
        "place_json": json_dumps(place),
        "active_context_id": active_context_id,
        "active_context_digest": wanted_digest,
        "ready_horizons_json": json_dumps(ready),
        "refresh_reason_json": json_dumps({"reason": reason, "force": force}),
        "context_payload_json": json_dumps(context),
        "built_latency_ms": latency_ms,
        "expires_at_epoch": _epoch_from_now(ttl_s),
        "updated_at": now,
    }
    with connect() as con:
        upsert(con, "brainlive_hot_context_cache", row, "cache_id")
        con.commit()
    return {"status": "refreshed", "active_context_id": active_context_id, "context": context, "latency_ms": latency_ms, "ready_horizons": ready, "reason": reason}


def route_triggers_llm(live_session_id: str, *, fused: dict[str, Any], hot_context: dict[str, Any], target_ms: int = 12000, timeout: float = 3.5) -> dict[str, Any]:
    ensure_hotloop_schema()
    started = time.time()
    fused_id = fused.get("fused_id")
    try:
        out = OllamaJsonClient().require_json(
            "Tu es BrainLive Trigger Router. Tu décides quels horizons H0/H1/H2 doivent tourner maintenant. Tu ne fais aucune règle par mots-clés: tu utilises uniquement signaux fusionnés, contexte Brain2, incertitudes, potentiel proactif et budget temps. JSON strict.",
            json_dumps({
                "mission": "Décide observe/run_h0/run_h0_h1/run_h0_h1_h2. H0=0-10s, H1=10s-5min, H2=5min-2h. Si contexte insuffisant, observe et indique quoi précharger.",
                "fused_situation": fused,
                "hot_context_ready": {"active_context_id": hot_context.get("active_context_id"), "status": hot_context.get("status"), "ready_horizons": hot_context.get("ready_horizons")},
                "latency_budget_ms": target_ms,
                "rules": ["no regex", "no keyword psychology", "run H2 only if it can improve short future", "observe if no useful proactive value"],
            }),
            schema_hint=ROUTER_SCHEMA,
            timeout=timeout,
        )
        status = str(out.get("route_status") or "observe")
        horizons = out.get("triggered_horizons") if isinstance(out.get("triggered_horizons"), list) else []
        if status == "run_h0" and not horizons: horizons = ["H0"]
        if status == "run_h0_h1" and not horizons: horizons = ["H0", "H1"]
        if status == "run_h0_h1_h2" and not horizons: horizons = ["H0", "H1", "H2"]
        route_status = status if status in {"observe", "run_h0", "run_h0_h1", "run_h0_h1_h2"} else ("run_h0_h1_h2" if horizons else "observe")
    except Exception as exc:
        out = {"error": str(exc)[:1000], "llm_required": True}
        route_status = "observe"
        horizons = []
    latency_ms = int((time.time() - started) * 1000)
    route_id = stable_id("blroute", live_session_id, fused_id or "none", now_iso(), uuid4().hex)
    with connect() as con:
        upsert(con, "brainlive_hot_trigger_routes", {
            "route_id": route_id,
            "live_session_id": live_session_id,
            "fused_id": fused_id,
            "route_status": route_status,
            "triggered_horizons_json": json_dumps(horizons),
            "router_json": json_dumps(out),
            "evidence_json": json_dumps({"fused_confidence": fused.get("confidence"), "hot_context_status": hot_context.get("status")}),
            "latency_ms": latency_ms,
            "created_at": now_iso(),
        }, "route_id")
        con.commit()
    return {"route_id": route_id, "route_status": route_status, "triggered_horizons": horizons, "router": out, "latency_ms": latency_ms}


def _hot_session_owner(live_session_id: str) -> str:
    with connect() as con:
        row = con.execute("SELECT person_id FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,)).fetchone()
    if not row or not row["person_id"]:
        raise ValueError(f"unknown BrainLive session: {live_session_id}")
    return str(row["person_id"])


def _hot_source_key(*, fused: dict[str, Any], hot_context: dict[str, Any]) -> str:
    # The fused event is the primary idempotency boundary.  A context id is a
    # safe fallback for an observation-only route without a fused projection.
    fused_id = str(fused.get("fused_id") or "")
    if fused_id:
        return f"fused:{fused_id}"
    manifest = ((hot_context.get("context") or {}).get("context_manifest") or hot_context.get("manifest") or {})
    return "context:" + str(manifest.get("context_id") or hot_context.get("active_context_id") or stable_id("hot-source", hot_context))


def _bounded_fused_for_capsule(fused: dict[str, Any]) -> dict[str, Any]:
    """Keep sensor reality useful without re-copying raw transcript/history."""
    summary = fused.get("summary") if isinstance(fused.get("summary"), dict) else {}
    values = {
        "fused_id": fused.get("fused_id"),
        "person_id": fused.get("person_id"),
        "place": fused.get("place"),
        "confidence": fused.get("confidence"),
        "readiness": fused.get("readiness"),
        "llm_fusion_status": fused.get("llm_fusion_status"),
        "event_ids": (summary.get("event_ids") if isinstance(summary, dict) else None) or fused.get("event_ids"),
        "speech": list((summary.get("speech") if isinstance(summary, dict) else None) or [])[-4:],
        "vision": list((summary.get("vision") if isinstance(summary, dict) else None) or [])[-3:],
        "people": list((summary.get("people") if isinstance(summary, dict) else None) or [])[-6:],
    }
    raw = json_dumps(values)
    max_chars = max(800, min(8000, int(__import__("os").environ.get("MLOMEGA_V18_HOT_FUSED_MAX_CHARS", "4500"))))
    if len(raw) <= max_chars:
        return values
    # Do not make invalid partial JSON: retain a clear bounded text snapshot.
    return {"fused_id": fused.get("fused_id"), "truncated": True, "json_excerpt": raw[:max_chars], "json_sha256": stable_id("fused", raw)}


def _hot_capsule(
    *,
    live_session_id: str,
    person_id: str,
    fused: dict[str, Any],
    hot_context: dict[str, Any],
    route: dict[str, Any],
    target_ms: int,
    revision_reason: str | None = None,
    parent_capsule_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Persist the exact rendered live prompt before any LLM call.

    The source manifest may be richer than the prompt.  The persisted capsule
    retains provenance, while ``prompt_payload`` is the immutable bounded bytes
    used by the model and by every retry.
    """
    context = dict(hot_context.get("context") or {})
    manifest = context.get("context_manifest") or hot_context.get("manifest") or {}
    scope = manifest.get("scope") or {}
    as_of = str(scope.get("as_of") or now_iso())
    source_key = _hot_source_key(fused=fused, hot_context=hot_context)
    items = [dict(x) for x in (manifest.get("items") or []) if isinstance(x, dict)]
    turns = [x for x in items if x.get("source_table") == "brainlive_turn_buffer"]
    episode = dict(context.get("episode") or {})
    omissions = [dict(x) for x in (manifest.get("omitted_refs") or []) if isinstance(x, dict)]
    omissions.extend([dict(x) for x in (manifest.get("excluded_future_refs") or []) if isinstance(x, dict)])

    prompt_payload, meta = build_hot_capsule_payload(
        episode=episode,
        manifest=manifest,
        fused=fused,
        route=route,
        target_ms=target_ms,
    )
    rendered_manifest = dict(prompt_payload.get("manifest") or {})
    rendered_episode = dict(prompt_payload.get("episode") or {})
    capsule = persist_episode_capsule(
        person_id=person_id,
        live_session_id=live_session_id,
        source_key=source_key,
        as_of=str((rendered_manifest.get("scope") or {}).get("as_of") or as_of),
        turns=turns,
        summary_text=str(rendered_episode.get("summary") or ""),
        references=items,
        omissions=omissions,
        input_budget_chars=int(meta["input_budget_chars"]),
        output_budget_tokens=int(meta["output_budget_tokens"]),
        status="context_incomplete" if bool(rendered_manifest.get("incomplete")) else "ready",
        episode_start_at=rendered_episode.get("episode_start_at"),
        episode_end_at=rendered_episode.get("episode_end_at"),
        extra={
            "prompt_payload": prompt_payload,
            "prompt_meta": meta,
            "revision_reason": revision_reason,
            "parent_capsule_id": parent_capsule_id,
        },
    )
    record_capsule_prompt_rendering(
        capsule_id=str(capsule["capsule_id"]),
        person_id=person_id,
        live_session_id=live_session_id,
        input_budget_chars=int(meta["input_budget_chars"]),
        rendered_input_chars=int(meta["rendered_input_chars"]),
        output_budget_tokens=int(meta["output_budget_tokens"]),
        prompt_payload=prompt_payload,
        incomplete=bool(rendered_manifest.get("incomplete")),
        details={
            "capsule_version": prompt_payload.get("schema_version"),
            "omitted_ref_count": meta.get("omitted_ref_count"),
            "revision_reason": revision_reason,
            "parent_capsule_id": parent_capsule_id,
        },
    )
    return capsule, prompt_payload, meta

def _hot_output_contract(
    result: dict[str, Any],
    *,
    manifest: dict[str, Any],
    person_id: str,
    live_session_id: str,
    as_of: str,
    requested_horizons: list[str],
) -> dict[str, Any]:
    """Strict semantic gate before any hot fact, forecast or delivery write."""
    if not isinstance(result, dict):
        raise ContractValidationError("hot result must be a JSON object")
    required = {"world_state", "horizons", "active_predictions", "proactive_decision", "notes_for_brain2", "uncertainties"}
    allowed = required | {"needs_evidence"}
    if not required <= set(result) or set(result) - allowed:
        raise ContractValidationError(f"hot result keys mismatch: required={sorted(required)} got={sorted(result)}")

    def _finite_01(value: Any, field: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            raise ContractValidationError(f"{field} must be finite [0,1]")

    def _evidence(value: Any, field: str, *, required: bool = False) -> None:
        validate_resolvable_manifest_evidence(
            value,
            context_manifest=manifest,
            person_id=person_id,
            live_session_id=live_session_id,
            as_of=as_of,
            field=field,
            required=required,
        )

    world = result.get("world_state")
    if not isinstance(world, dict):
        raise ContractValidationError("world_state must be an object")
    _finite_01(world.get("confidence"), "world_state.confidence")
    has_world_claim = bool(
        world.get("where_am_i")
        or world.get("what_is_happening")
        or world.get("probable_activity")
        or str(world.get("active_mode") or "").strip().lower() not in {"", "unknown"}
    )
    _evidence(world.get("evidence"), "world_state.evidence", required=has_world_claim)
    _evidence(world.get("counter_evidence") or [], "world_state.counter_evidence")

    requested_evidence = result.get("needs_evidence")
    if requested_evidence is None:
        requested_evidence = []
    if not isinstance(requested_evidence, list) or len(requested_evidence) > 4:
        raise ContractValidationError("needs_evidence must be a bounded list of at most four announced references")
    _evidence(requested_evidence, "needs_evidence")

    horizons = result.get("horizons")
    if not isinstance(horizons, dict) or set(horizons) != {"H0", "H1", "H2"}:
        raise ContractValidationError("hot horizons must contain exactly H0/H1/H2")
    for horizon, payload in horizons.items():
        if not isinstance(payload, dict):
            raise ContractValidationError(f"{horizon} must be an object")
        _finite_01(payload.get("confidence"), f"{horizon}.confidence")
        nonempty = any(
            payload.get(key)
            for key in ("summary", "needs", "risks_or_opportunities", "intervention_candidates", "watch_next")
        )
        if horizon not in requested_horizons and nonempty:
            raise ContractValidationError(f"{horizon} output was not requested by router")
        _evidence(payload.get("evidence") or [], f"{horizon}.evidence", required=bool(payload.get("summary")))
        _evidence(payload.get("counter_evidence") or [], f"{horizon}.counter_evidence")

    predictions = result.get("active_predictions")
    if not isinstance(predictions, list):
        raise ContractValidationError("active_predictions must be a list")
    for idx, prediction in enumerate(predictions):
        if not isinstance(prediction, dict):
            raise ContractValidationError(f"active_predictions[{idx}] must be object")
        horizon = str(prediction.get("horizon") or "")
        if horizon not in {"H0", "H1", "H2"} or horizon not in requested_horizons:
            raise ContractValidationError(f"active_predictions[{idx}] has invalid/unrequested horizon")
        if not str(prediction.get("prediction") or "").strip():
            raise ContractValidationError(f"active_predictions[{idx}] missing prediction")
        _finite_01(prediction.get("probability"), f"active_predictions[{idx}].probability")
        _finite_01(prediction.get("confidence"), f"active_predictions[{idx}].confidence")
        _evidence(prediction.get("evidence"), f"active_predictions[{idx}].evidence", required=True)
        _evidence(prediction.get("counter_evidence") or [], f"active_predictions[{idx}].counter_evidence")

    proactive = result.get("proactive_decision")
    if not isinstance(proactive, dict):
        raise ContractValidationError("proactive_decision must be object")
    decision = str(proactive.get("decision") or "observe")
    if decision not in {"observe", "speak_now", "queue", "wait"}:
        raise ContractValidationError("proactive decision enum invalid")
    if decision in {"speak_now", "queue"}:
        if not str(proactive.get("message") or "").strip():
            raise ContractValidationError("proactive delivery needs a message")
        # The delivery primitive is intentionally H1-owned. A fast H0 signal
        # can motivate it, but may not bypass the common H1 policy/queue gate.
        if str(proactive.get("horizon") or "") != "H1":
            raise ContractValidationError("proactive delivery must be H1-owned")
        if "H1" not in requested_horizons:
            raise ContractValidationError("proactive delivery requires requested H1")
        _evidence(proactive.get("evidence"), "proactive_decision.evidence", required=True)
        _evidence(proactive.get("counter_evidence") or [], "proactive_decision.counter_evidence")
    for field in ("expected_gain", "intrusion_cost", "confidence"):
        _finite_01(proactive.get(field), f"proactive_decision.{field}")
    result["needs_evidence"] = requested_evidence
    return result

def _record_hot_success(
    *,
    decision_run_id: str,
    live_session_id: str,
    fused: dict[str, Any],
    hot_context: dict[str, Any],
    route: dict[str, Any],
    result: dict[str, Any],
    latency_ms: int,
    target_ms: int,
    source_key: str,
) -> tuple[str, int, list[str]]:
    """Persist projections and the Bridge-consumed queue before marking success.

    Queue-before-success is intentional.  A crash after the queue commit merely
    reclaims this decision run and returns the same dedupe winner; a crash after
    success can never erase a delivery.
    """
    ensure_delivery_schema()
    budget_id = stable_id("blbudget-v18", decision_run_id)
    created_at = now_iso()
    delivered: list[str] = []
    with connect() as con, write_transaction(con):
        upsert(con, "brainlive_hot_budget_runs", {
            "budget_run_id": budget_id,
            "live_session_id": live_session_id,
            "fused_id": fused.get("fused_id"),
            "route_id": route.get("route_id"),
            "target_total_ms": int(target_ms),
            "observed_total_ms": int(latency_ms),
            "stages_json": json_dumps({"decision_run_id": decision_run_id, "durable_v18": True}),
            "latency_ok": 1 if latency_ms <= target_ms else 0,
            "status": "ok",
            "result_json": json_dumps(result),
            "created_at": created_at,
        }, "budget_run_id")
        materialized = _materialize_active_predictions(
            con, live_session_id=live_session_id, budget_run_id=budget_id,
            fused=fused, hot_context=hot_context, result=result, now=created_at,
        )
        proactive = result.get("proactive_decision") if isinstance(result, dict) else {}
        if isinstance(proactive, dict) and str(proactive.get("decision") or "") in {"speak_now", "queue"} and proactive.get("message"):
            iid = stable_id("blhotint-v18", decision_run_id, proactive.get("message"))
            queue_candidate = {**proactive, "candidate_id": proactive.get("candidate_id") or iid, "decision": "queue"}
            queue_result = enqueue_delivery(
                live_session_id=live_session_id,
                source_key=source_key,
                candidate=queue_candidate,
                decision_run_id=decision_run_id,
                hot_intervention_id=iid,
                tick_id=route.get("route_id"),
                con=con,
                schema_ready=True,
            )
            delivery_id = queue_result.get("delivery_id")
            delivery_status = str(queue_result.get("status") or "skipped")
            upsert(con, "brainlive_hot_intervention_log", {
                "hot_intervention_id": iid,
                "live_session_id": live_session_id,
                "budget_run_id": budget_id,
                "horizon": "H1",  # one explicit delivery owner regardless of prediction horizon
                "decision": str(proactive.get("decision")),
                "message": proactive.get("message"),
                "expected_gain": _clamp(proactive.get("expected_gain")),
                "confidence": _clamp(proactive.get("confidence")),
                "reason_json": json_dumps({**proactive, "decision_run_id": decision_run_id, "queue_result": queue_result}),
                "delivery_status": delivery_status,
                "created_at": created_at,
            }, "hot_intervention_id")
            if delivery_id:
                delivered.append(str(delivery_id))
    return budget_id, materialized, list(dict.fromkeys(delivered))



def _schedule_evidence_revision(
    *,
    decision_run_id: str,
    parent_capsule_id: str,
    live_session_id: str,
    person_id: str,
    fused: dict[str, Any],
    hot_context: dict[str, Any],
    route: dict[str, Any],
    capsule_payload: dict[str, Any],
    requested_refs: list[dict[str, Any]],
    target_ms: int,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a *new*, explicitly linked capsule after a model asks for proof.

    H0 does not wait for this path.  The current decision becomes terminally
    visible as ``needs_evidence`` and the next service worker pass executes the
    successor with the same episode plus the bounded, explicitly requested rows.
    """
    manifest = dict(capsule_payload.get("manifest") or {})
    episode = dict(capsule_payload.get("episode") or {})
    record_llm_evidence_requests(
        decision_run_id=decision_run_id,
        capsule_id=parent_capsule_id,
        person_id=person_id,
        live_session_id=live_session_id,
        refs=requested_refs,
        reason="hot_model_needs_announced_evidence",
    )
    from .v18_context import retrieve_context_references

    resolved = retrieve_context_references(
        person_id=person_id,
        live_session_id=live_session_id,
        manifest=manifest,
        refs=requested_refs,
        max_items=4,
        max_chars=3_200,
    )
    if len(resolved) != len(requested_refs):
        raise ContractValidationError("requested evidence could not be resolved from immutable capsule")
    resolve_llm_evidence_requests(decision_run_id=decision_run_id, resolved=resolved)
    revised_manifest = dict(manifest)
    revised_items: list[dict[str, Any]] = []
    resolved_by_key = {(str(item.get("source_table")), str(item.get("source_id"))): item for item in resolved}
    for item in list(manifest.get("items") or []):
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        found = resolved_by_key.get((str(copy.get("source_table")), str(copy.get("source_id"))))
        if found:
            copy["text"] = found.get("text") or ""
            copy["retrievable"] = True
            copy["truncated"] = bool(found.get("truncated"))
        revised_items.append(copy)
    revised_manifest["items"] = revised_items
    revised_manifest["incomplete"] = False
    revised_context = {
        "context": {
            "context_manifest": revised_manifest,
            "episode": episode,
        },
        "active_context_id": hot_context.get("active_context_id"),
    }
    capsule, prompt_payload, _meta = _hot_capsule(
        live_session_id=live_session_id,
        person_id=person_id,
        fused=fused,
        hot_context=revised_context,
        route=route,
        target_ms=target_ms,
        revision_reason="model_requested_evidence",
        parent_capsule_id=parent_capsule_id,
    )
    successor = ensure_llm_decision_run(
        person_id=person_id,
        live_session_id=live_session_id,
        source_key=str(capsule["source_key"]),
        capsule_id=str(capsule["capsule_id"]),
        capsule_hash=str(capsule["capsule_hash"]),
        execution_mode="hot_unified_v18_3",
        contract_version="hot-unified-semantic-v18.3",
        model=getattr(OllamaJsonClient, "model", None),
        generation={
            "fused": _bounded_fused_for_capsule(fused),
            "route": route,
            "target_ms": int(target_ms),
            "timeout": float(timeout),
            "parent_decision_run_id": decision_run_id,
            "revision_reason": "model_requested_evidence",
        },
        max_attempts=5,
    )
    with connect() as con, write_transaction(con):
        con.execute(
            """UPDATE v18_llm_evidence_requests
               SET state='resolved',resolution_json=?,resolved_at=?,updated_at=?
               WHERE decision_run_id=? AND state='requested'""",
            (json_dumps({"resolved": resolved}), now_iso(), now_iso(), decision_run_id),
        )
    return successor, prompt_payload

def _execute_hot_decision(
    *,
    decision_run: dict[str, Any],
    live_session_id: str,
    fused: dict[str, Any],
    hot_context: dict[str, Any],
    route: dict[str, Any],
    capsule_payload: dict[str, Any],
    target_ms: int,
    timeout: float,
) -> dict[str, Any]:
    lease = claim_llm_decision_run(decision_run_id=str(decision_run["decision_run_id"]), lease_seconds=max(30, int(timeout * 3)))
    if not lease:
        current = None
        with connect() as con:
            row = con.execute("SELECT * FROM v18_llm_decision_runs WHERE decision_run_id=?", (decision_run["decision_run_id"],)).fetchone()
            current = dict(row) if row else None
        return {"status": "deferred", "decision_run_id": decision_run["decision_run_id"], "decision_state": (current or {}).get("state")}
    phase = str(lease.get("phase") or "initial")
    manifest = dict(capsule_payload.get("manifest") or {})
    as_of = str((manifest.get("scope") or {}).get("as_of") or capsule_payload.get("as_of") or now_iso())
    horizons = list((route.get("triggered_horizons") or []))
    started = time.time()
    if bool(manifest.get("incomplete")):
        saved = finish_llm_decision_run(
            decision_run_id=str(lease["decision_run_id"]),
            lease_token=str(lease["lease_token"]),
            outcome="quarantined",
            result={"status": "context_incomplete", "capsule_id": lease.get("capsule_id")},
            error_kind="context_incomplete",
            error_text="hot inference refused an unresolvable/incomplete episode capsule",
        )
        return {
            "status": "context_incomplete",
            "decision_run_id": lease["decision_run_id"],
            "decision_state": saved.get("state"),
            "delivery_ids": [],
        }
    raw_output = ""
    try:
        system = "You are BrainLive Hot Prediction V18.5. Return strict JSON only. Every non-empty claim must cite evidence as {source_table,source_id} from the supplied capsule manifest. Do not cite unannounced history. If a named manifest reference needs its full content, return only up to four needs_evidence references and do not fabricate a claim."
        # The bounded capsule is the exact user payload.  Do not wrap it in a
        # second duplicated context object: that was the subtle path by which a
        # nominal capsule budget could still yield an oversized live prompt.
        # Stable behavioral rules live in the system message; the immutable
        # capsule contains the episode, route and output budget already.
        prompt = dict(capsule_payload)
        if phase == "repair":
            system += " Previous output violated the contract. Rebuild from the same capsule without adding facts or sources; this is the one permitted repair."
        client = OllamaJsonClient()
        result = client.require_json(system, json_dumps(prompt), schema_hint=HOT_UNIFIED_SCHEMA, timeout=timeout, max_output_tokens=int((capsule_payload.get("output_budget_tokens") or 900)))
        # `require_json` returns a valid object; retain a canonical raw copy for
        # audit even though provider raw is not surfaced on success.
        raw_output = json_dumps(result)
        result = _hot_output_contract(
            result,
            manifest=manifest,
            person_id=str(lease["person_id"]),
            live_session_id=live_session_id,
            as_of=as_of,
            requested_horizons=horizons,
        )
        requested_refs = list(result.get("needs_evidence") or [])
        if requested_refs:
            successor, _successor_payload = _schedule_evidence_revision(
                decision_run_id=str(lease["decision_run_id"]),
                parent_capsule_id=str(lease["capsule_id"]),
                live_session_id=live_session_id,
                person_id=str(lease["person_id"]),
                fused=fused,
                hot_context=hot_context,
                route=route,
                capsule_payload=capsule_payload,
                requested_refs=[dict(item) for item in requested_refs],
                target_ms=target_ms,
                timeout=timeout,
            )
            saved = finish_llm_decision_run(
                decision_run_id=str(lease["decision_run_id"]),
                lease_token=str(lease["lease_token"]),
                outcome="terminal_error",
                result={"status": "needs_evidence", "successor_decision_run_id": successor["decision_run_id"], "requested_refs": requested_refs},
                raw_output=raw_output,
                error_kind="needs_evidence",
                error_text="model requested announced evidence; successor capsule scheduled",
            )
            return {"status": "needs_evidence", "decision_run_id": lease["decision_run_id"], "successor_decision_run_id": successor["decision_run_id"], "decision_state": saved.get("state"), "delivery_ids": []}
        elapsed = int((time.time() - started) * 1000)
        budget_id, materialized, delivery_ids = _record_hot_success(
            decision_run_id=str(lease["decision_run_id"]), live_session_id=live_session_id, fused=fused,
            hot_context=hot_context, route=route, result=result, latency_ms=elapsed, target_ms=target_ms,
            source_key=str(lease["source_key"]),
        )
        finish_llm_decision_run(
            decision_run_id=str(lease["decision_run_id"]), lease_token=str(lease["lease_token"]),
            outcome="succeeded", result={"budget_run_id": budget_id, "delivery_ids": delivery_ids, "materialized_forecasts": materialized, "result": result}, raw_output=raw_output,
        )
        return {"status": "ok", "decision_run_id": lease["decision_run_id"], "budget_run_id": budget_id, "result": result, "latency_ms": elapsed, "latency_ok": elapsed <= target_ms, "materialized_forecasts": materialized, "delivery_ids": delivery_ids, "phase": phase}
    except Exception as exc:
        elapsed = int((time.time() - started) * 1000)
        raw_output = raw_output or str(getattr(exc, "raw", "") or "")
        kind = classify_llm_exception(exc)
        # A malformed or clipped output gets one explicit repair on the same
        # capsule. Timeouts/transport faults use durable backoff. No path turns
        # an error into a synthetic empty cognitive success.
        if kind in {"truncated_output", "invalid_contract"}:
            outcome = "repair_requested" if phase != "repair" else "quarantined"
        elif kind == "transient_runtime_error":
            outcome = "retryable_error"
        else:
            outcome = "terminal_error"
        saved = finish_llm_decision_run(
            decision_run_id=str(lease["decision_run_id"]), lease_token=str(lease["lease_token"]), outcome=outcome,
            result={"status": outcome, "latency_ms": elapsed}, raw_output=raw_output, error_kind=kind, error_text=str(exc)[:3500], retry_delay_seconds=20,
        )
        return {"status": outcome, "decision_run_id": lease["decision_run_id"], "error": str(exc)[:1500], "error_kind": kind, "latency_ms": elapsed, "decision_state": saved.get("state"), "phase": phase}


def run_unified_hot_prediction(
    live_session_id: str,
    *,
    fused: dict[str, Any],
    hot_context: dict[str, Any],
    route: dict[str, Any],
    target_ms: int = 12000,
    timeout: float = 9.0,
) -> dict[str, Any]:
    """One fast, bounded LLM call with durable retry/replay ownership.

    It keeps the live one-call performance model while replacing the former
    fire-and-forget monolith with an immutable episode capsule and a durable
    decision run.  The H1 delivery queue is written before the run succeeds.
    """
    ensure_hotloop_schema()
    horizons = list(route.get("triggered_horizons") or [])
    if not horizons:
        return {"status": "observe", "reason": "router_observe", "route": route, "delivery_ids": []}
    person_id = _hot_session_owner(live_session_id)
    capsule, payload, _capsule_meta = _hot_capsule(live_session_id=live_session_id, person_id=person_id, fused=fused, hot_context=hot_context, route=route, target_ms=target_ms)
    run = ensure_llm_decision_run(
        person_id=person_id, live_session_id=live_session_id, source_key=str(capsule["source_key"]),
        capsule_id=str(capsule["capsule_id"]), capsule_hash=str(capsule["capsule_hash"]), execution_mode="hot_unified_v18_3",
        contract_version="hot-unified-semantic-v18.3", model=getattr(OllamaJsonClient, "model", None),
        generation={"fused": _bounded_fused_for_capsule(fused), "route": route, "target_ms": int(target_ms), "timeout": float(timeout)},
        max_attempts=5,
    )
    return _execute_hot_decision(decision_run=run, live_session_id=live_session_id, fused=fused, hot_context=hot_context, route=route, capsule_payload=payload, target_ms=target_ms, timeout=timeout)


def drain_due_hot_llm_decisions(*, live_session_id: str | None = None, limit: int = 4) -> list[dict[str, Any]]:
    """Run due hot LLM retries even when no new sensor signal arrives."""
    ensure_hotloop_schema()
    params: list[Any] = ["pending", "retryable_error", "repair_requested"]
    sql = "SELECT * FROM v18_llm_decision_runs WHERE state IN (?,?,?)"
    if live_session_id:
        sql += " AND live_session_id=?"
        params.append(live_session_id)
    sql += " ORDER BY updated_at ASC LIMIT ?"
    params.append(max(1, int(limit)))
    with connect() as con:
        rows = [dict(row) for row in con.execute(sql, tuple(params)).fetchall()]
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            capsule = None
            with connect() as con:
                cap_row = con.execute("SELECT capsule_json FROM v18_episode_capsules WHERE capsule_id=?", (row["capsule_id"],)).fetchone()
            if not cap_row:
                continue
            capsule_payload = json_loads(cap_row["capsule_json"], {}) or {}
            generation = json_loads(row.get("generation_json"), {}) or {}
            fused = generation.get("fused") or {}
            route = generation.get("route") or {"triggered_horizons": ["H1"]}
            target_ms = int(generation.get("target_ms") or 12000)
            timeout = float(generation.get("timeout") or 9.0)
            # Retries use the exact immutable rendered prompt, never a newer
            # active-context rebuild around an older source signal.
            extra = capsule_payload.get("extra") if isinstance(capsule_payload.get("extra"), dict) else {}
            payload = extra.get("prompt_payload") if isinstance(extra.get("prompt_payload"), dict) else capsule_payload
            hot_context = {"context": {"context_manifest": payload.get("manifest") or {}, "episode": payload.get("episode") or {}}}
            results.append(_execute_hot_decision(decision_run=row, live_session_id=str(row["live_session_id"]), fused=fused, hot_context=hot_context, route=route, capsule_payload=payload, target_ms=target_ms, timeout=timeout))
        except Exception as exc:
            results.append({"status": "worker_error", "decision_run_id": row.get("decision_run_id"), "error": str(exc)[:1000]})
    return results

def hot_brainlive_cycle(
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
) -> dict[str, Any]:
    """The intended V15.6 flow: identify/cache -> preheat Brain2 -> fuse -> route -> predict."""
    ensure_hotloop_schema()
    stages: dict[str, Any] = {}
    t0 = time.time()
    speaker = resolve_speaker_hot(live_session_id, audio_sample_path=latest_audio_sample_path, explicit_person_id=explicit_speaker_person_id, speaker_label=latest_speaker_label)
    stages["identity_ms"] = int((time.time() - t0) * 1000)
    active_people = [speaker["person_id"]] if speaker.get("person_id") else []
    t_place = time.time()
    place = resolve_place_multisource(live_session_id, explicit_location=explicit_location, gps_json=gps_json, person_id=person_id)
    stages["place_ms"] = int((time.time() - t_place) * 1000)
    # Always keep context hot on person/place changes; if no signal, just prepare.
    t_ctx = time.time()
    hot_ctx = prepare_hot_context(live_session_id, person_id=person_id, active_people=active_people, place=place, reason="meaningful_signal" if meaningful_signal else "cadence_preheat", force=force_context or meaningful_signal, ttl_s=45.0)
    stages["hot_context_total_ms"] = int((time.time() - t_ctx) * 1000)
    if not meaningful_signal:
        return {"status": "hot_context_ready", "speaker": speaker, "place": place, "hot_context": {k: v for k, v in hot_ctx.items() if k != "context"}, "stages": stages}
    t_fused = time.time()
    fused = build_fused_situation(live_session_id, person_id=person_id, explicit_location=explicit_location, gps_json=gps_json, force_context_refresh=False, use_llm=True)
    stages["fuse_ms"] = int((time.time() - t_fused) * 1000)
    route = route_triggers_llm(live_session_id, fused=fused, hot_context=hot_ctx, target_ms=12000)
    prediction = run_unified_hot_prediction(live_session_id, fused=fused, hot_context=hot_ctx, route=route, target_ms=12000, timeout=9.0)
    prediction["stages"] = {**stages, **(prediction.get("stages") or {})}
    return {"status": prediction.get("status"), "speaker": speaker, "place": place, "hot_context": {k: v for k, v in hot_ctx.items() if k != "context"}, "fused": fused, "route": route, "prediction": prediction, "stages": prediction["stages"]}
