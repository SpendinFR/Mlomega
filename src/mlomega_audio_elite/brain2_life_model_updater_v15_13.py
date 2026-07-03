from __future__ import annotations

"""V15.13 Brain2 Life Model updater with strata and patch semantics.

This module fixes the unsafe pattern of rebuilding William's whole life model on
one nightly prompt. It makes Brain2's life model behave like a stable memory:

- General model: slow, high-confidence identity/routines/preferences.
- Recent model: last weeks/months; can shift but requires evidence.
- Very recent model: last 24-72h; useful for BrainLive but not yet truth.

The LLM is used as a *patch proposer*, not as an unrestricted rewriter. It sees:
current life model snapshots + new evidence delta + outcomes/reconciliations and
must return operations such as create, confirm, update, weaken, contradict,
obsolete or keep. Deterministic code stores patch runs, lifecycle rows and
stratified snapshots. BrainLive should prefer active/confirmed hooks and treat
very_recent/candidate items as watch-only unless confidence is high.

Strict policy: no regex/keyword psychology. Deterministic code can count,
window, link and apply lifecycle metadata. Psychological/intention/need meaning
comes from Brain2 LLM outputs or existing evidence with explicit confidence.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id
from .brain2_life_model_v15_10 import (
    CANONICAL_SCHEMA,
    collect_canonical_evidence,
    ensure_life_model_schema,
    build_brain2_canonical_life_model,
    store_canonical_life_model,
)

VERSION = "15.13.0-brain2-life-model-updater-stratified"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brain2_life_model_patch_runs(
  patch_run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  status TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  current_model_digest TEXT,
  delta_counts_json TEXT DEFAULT '{}',
  patch_json TEXT DEFAULT '{}',
  error_text TEXT,
  llm_model TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS brain2_life_model_patch_operations(
  operation_id TEXT PRIMARY KEY,
  patch_run_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  op TEXT NOT NULL,
  target_layer TEXT NOT NULL,
  target_table TEXT,
  target_id TEXT,
  identity_key TEXT,
  stratum TEXT NOT NULL DEFAULT 'recent',
  reason TEXT,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence_before REAL,
  confidence_after REAL,
  confidence_delta REAL DEFAULT 0.0,
  patch_data_json TEXT DEFAULT '{}',
  lifecycle_json TEXT DEFAULT '{}',
  live_effect_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'applied',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brain2_life_model_strata(
  stratum_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  stratum TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  model_json TEXT DEFAULT '{}',
  evidence_window_start TEXT,
  evidence_window_end TEXT,
  patch_run_id TEXT,
  source_counts_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brain2_life_model_item_lifecycle(
  lifecycle_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  layer TEXT NOT NULL,
  identity_key TEXT,
  stratum TEXT NOT NULL DEFAULT 'recent',
  truth_status TEXT NOT NULL DEFAULT 'candidate',
  first_seen_at TEXT,
  last_seen_at TEXT,
  last_confirmed_at TEXT,
  last_contradicted_at TEXT,
  evidence_count INTEGER DEFAULT 0,
  counter_evidence_count INTEGER DEFAULT 0,
  confidence REAL DEFAULT 0.5,
  recency_weight REAL DEFAULT 1.0,
  staleness_score REAL DEFAULT 0.0,
  valid_from TEXT,
  valid_until TEXT,
  superseded_by TEXT,
  obsolete_reason TEXT,
  use_policy TEXT DEFAULT 'watch_only',
  notes_json TEXT DEFAULT '{}',
  updated_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brain2_life_model_delta_evidence(
  delta_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  status TEXT NOT NULL,
  source_counts_json TEXT DEFAULT '{}',
  raw_evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_b2_life_patch_person ON brain2_life_model_patch_runs(person_id, created_at);
CREATE INDEX IF NOT EXISTS idx_b2_life_ops_person ON brain2_life_model_patch_operations(person_id, target_layer, op, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_b2_life_strata_unique ON brain2_life_model_strata(person_id, stratum);
CREATE UNIQUE INDEX IF NOT EXISTS idx_b2_life_item_lifecycle_unique ON brain2_life_model_item_lifecycle(person_id, source_table, source_id, stratum);
"""

PATCH_SCHEMA: dict[str, Any] = {
    "patch_intent": "incremental_update_not_rewrite",
    "operations": [
        {
            "op": "create|confirm|update|weaken|contradict|obsolete|keep",
            "target_layer": "routine|place|action_preference|need_expectation|expression_state|emotional_trajectory|contextual_self|live_prediction_hook|affordance_preference",
            "target_id": "optional existing id",
            "identity_key": "stable human-readable key",
            "stratum": "general|recent|very_recent",
            "reason": "why this operation is justified by new evidence",
            "evidence": [],
            "counter_evidence": [],
            "confidence_before": 0.0,
            "confidence_after": 0.0,
            "confidence_delta": 0.0,
            "patch_data": {},
            "lifecycle": {
                "truth_status": "candidate|active|confirmed|weakened|contradicted|obsolete|superseded",
                "use_policy": "do_not_use|watch_only|silent_context|proactive_allowed|strong_live_hook",
                "valid_from": "optional iso datetime",
                "valid_until": "optional iso datetime",
                "obsolete_reason": "optional"
            },
            "live_effect": {
                "horizons": ["H0", "H1", "H2", "day", "week", "long"],
                "brainlive_action": "watch|preload_context|activate_hook|allow_intervention|avoid_intervention",
                "notes_for_brainlive": []
            }
        }
    ],
    "strata_guidance": {
        "general": "slow/stable model; change only with repeated/strong evidence",
        "recent": "last weeks/months; active tendencies, can move faster",
        "very_recent": "last 24-72h; mostly watch-only unless strongly confirmed"
    },
    "missing_evidence_for_magic": [],
    "do_not_update_without": [],
    "summary_for_brainlive": []
}

LAYER_TO_CANONICAL_KEY = {
    "routine": "personal_routine_models",
    "place": "place_preference_models",
    "action_preference": "action_preference_models",
    "need_expectation": "need_expectation_models",
    "expression_state": "expression_state_models",
    "emotional_trajectory": "emotional_trajectory_models",
    "contextual_self": "contextual_self_models",
    "live_prediction_hook": "live_prediction_hooks",
    "affordance_preference": "live_affordance_preferences",
}

CANONICAL_TABLES: dict[str, tuple[str, str, str]] = {
    "routine": ("brain2_personal_routine_models", "routine_id", "routine_name"),
    "place": ("brain2_place_preference_models", "place_model_id", "place_key"),
    "action_preference": ("brain2_action_preference_models", "action_model_id", "action_or_choice"),
    "need_expectation": ("brain2_need_expectation_models", "need_model_id", "need_or_expectation"),
    "expression_state": ("brain2_expression_state_models", "expression_model_id", "expression_or_style"),
    "emotional_trajectory": ("brain2_emotional_trajectory_models", "trajectory_model_id", "trajectory_name"),
    "contextual_self": ("brain2_contextual_self_models", "contextual_model_id", "context_key"),
    "live_prediction_hook": ("brain2_live_prediction_hooks", "hook_id", "hook_name"),
    "affordance_preference": ("brain2_live_affordance_preferences", "affordance_pref_id", "affordance_type"),
}


def ensure_life_model_updater_schema() -> None:
    ensure_life_model_schema()
    init_db()
    with connect() as con:
        con.executescript(SCHEMA)
        con.commit()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _clamp(v: Any, default: float = 0.5) -> float:
    try:
        x = float(v)
    except Exception:
        x = default
    return max(0.0, min(1.0, x))


def _list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else ([] if v in (None, "") else [v])


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _query(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _count(feed: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for k, v in feed.items():
        if isinstance(v, dict):
            counts[k] = sum(len(x) if isinstance(x, list) else (1 if x else 0) for x in v.values())
        elif isinstance(v, list):
            counts[k] = len(v)
        else:
            counts[k] = 1 if v else 0
    return counts


def _safe_json(v: Any, default: Any) -> Any:
    return json_loads(v, default) if isinstance(v, str) else (v if v is not None else default)


def _compact_rows(rows: list[dict[str, Any]], limit: int = 80) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        r = dict(row)
        for k, v in list(r.items()):
            if isinstance(v, str) and len(v) > 1600:
                r[k] = v[:1600] + "…"
        out.append(r)
    return out


def load_current_life_model(person_id: str, *, limit: int = 80) -> dict[str, Any]:
    """Load existing canonical models plus lifecycle/strata snapshots."""
    ensure_life_model_updater_schema()
    current: dict[str, Any] = {"person_id": person_id, "canonical_layers": {}, "strata": {}, "lifecycle": {}}
    with connect() as con:
        for layer, (table, id_col, _name_col) in CANONICAL_TABLES.items():
            if _table_exists(con, table):
                current["canonical_layers"][layer] = _compact_rows(_query(con, f"SELECT * FROM {table} WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit)
            else:
                current["canonical_layers"][layer] = []
        if _table_exists(con, "brain2_life_model_strata"):
            for row in _query(con, "SELECT * FROM brain2_life_model_strata WHERE person_id=? ORDER BY updated_at DESC", (person_id,)):
                current["strata"][row.get("stratum") or "unknown"] = _safe_json(row.get("model_json"), {})
        if _table_exists(con, "brain2_life_model_item_lifecycle"):
            rows = _query(con, "SELECT * FROM brain2_life_model_item_lifecycle WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit * 2))
            current["lifecycle"] = _compact_rows(rows, limit * 2)
        if _table_exists(con, "brain2_life_model_exports"):
            row = con.execute("SELECT export_id,status,created_at,source_counts_json FROM brain2_life_model_exports WHERE person_id=? ORDER BY created_at DESC LIMIT 1", (person_id,)).fetchone()
            if row:
                current["latest_export"] = {"export_id": row["export_id"], "status": row["status"], "created_at": row["created_at"], "source_counts": _safe_json(row["source_counts_json"], {})}
    return current


def collect_life_model_delta(person_id: str, *, period_start: str | None = None, period_end: str | None = None, limit: int = 120) -> dict[str, Any]:
    """Collect only new/relevant evidence windows; no psychological synthesis."""
    ensure_life_model_updater_schema()
    now_dt = _now_dt()
    if period_end is None:
        period_end = _iso(now_dt)
    if period_start is None:
        # Default to last 24h for a nightly patch. General/recent windows are sent
        # separately as context; this delta is what can change the model today.
        period_start = _iso(now_dt - timedelta(days=1))
    delta = collect_canonical_evidence(person_id, period_start=period_start, period_end=period_end, limit=limit)
    # Add live-day packages and reconciliations explicitly because they are the
    # bridge from BrainLive outcomes back into Brain2.
    with connect() as con:
        live: dict[str, Any] = {}
        if _table_exists(con, "brainlive_day_packages"):
            live["day_packages"] = _compact_rows(_query(con, "SELECT * FROM brainlive_day_packages WHERE person_id=? AND COALESCE(period_end, updated_at, created_at) >= ? ORDER BY created_at DESC LIMIT ?", (person_id, period_start, limit)), limit)
        if _table_exists(con, "brainlive_brain2_reconciliations"):
            live["reconciliations"] = _compact_rows(_query(con, "SELECT * FROM brainlive_brain2_reconciliations WHERE person_id=? AND updated_at >= ? ORDER BY updated_at DESC LIMIT ?", (person_id, period_start, limit)), limit)
        if _table_exists(con, "brainlive_context_snapshots_v1512"):
            live["context_snapshots"] = _compact_rows(_query(con, "SELECT * FROM brainlive_context_snapshots_v1512 WHERE person_id=? AND created_at >= ? ORDER BY created_at DESC LIMIT ?", (person_id, period_start, limit)), limit)
        # V15.14: full BrainLive event bundles are the preferred offline evidence
        # for Life Model updates because they preserve transcripts, diarization,
        # vision descriptions, world states, predictions, interventions and outcomes
        # as one scene instead of short live fragments.
        if _table_exists(con, "brainlive_event_bundles_v1514"):
            live["event_bundles"] = _compact_rows(_query(con, "SELECT * FROM brainlive_event_bundles_v1514 WHERE person_id=? AND COALESCE(end_time, start_time, updated_at, created_at) >= ? ORDER BY COALESCE(start_time, created_at) DESC LIMIT ?", (person_id, period_start, limit)), limit)
        if _table_exists(con, "brainlive_brain2_event_exports_v1514"):
            live["event_exports_to_brain2"] = _compact_rows(_query(con, "SELECT * FROM brainlive_brain2_event_exports_v1514 WHERE person_id=? AND export_status IN ('active','ok','exported') AND updated_at >= ? ORDER BY updated_at DESC LIMIT ?", (person_id, period_start, limit)), limit)
        # V16.0: non-verbal/silent routines and activities created from BrainLive
        # vision/place/world-state evidence. These are important when there was no
        # conversation: computer work, cigarette/pause, resting, walking, waiting.
        if _table_exists(con, "brainlive_silent_event_candidates_v160"):
            live["silent_nonverbal_candidates_v160"] = _compact_rows(_query(con, "SELECT * FROM brainlive_silent_event_candidates_v160 WHERE person_id=? AND COALESCE(end_time, start_time, updated_at, created_at) >= ? ORDER BY COALESCE(start_time, created_at) DESC LIMIT ?", (person_id, period_start, limit)), limit)
        delta["brainlive_bridge_delta"] = live
        delta_id = stable_id("b2delta", person_id, period_start, period_end)
        upsert(con, "brain2_life_model_delta_evidence", {
            "delta_id": delta_id, "person_id": person_id, "period_start": period_start, "period_end": period_end,
            "status": "ready", "source_counts_json": json_dumps(_count(delta)), "raw_evidence_json": json_dumps(delta), "created_at": now_iso(),
        }, "delta_id")
        con.commit()
    return delta


def _summarize_strata(person_id: str) -> dict[str, Any]:
    """Build deterministic strata snapshots from canonical tables+lifecycle."""
    ensure_life_model_updater_schema()
    with connect() as con:
        strata: dict[str, Any] = {"general": {}, "recent": {}, "very_recent": {}}
        for layer, (table, id_col, name_col) in CANONICAL_TABLES.items():
            if not _table_exists(con, table):
                for s in strata:
                    strata[s][layer] = []
                continue
            rows = _query(con, f"SELECT * FROM {table} WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT 80", (person_id,))
            lifecycle_map: dict[tuple[str, str], dict[str, Any]] = {}
            if _table_exists(con, "brain2_life_model_item_lifecycle"):
                for lc in _query(con, "SELECT * FROM brain2_life_model_item_lifecycle WHERE person_id=? AND source_table=?", (person_id, table)):
                    lifecycle_map[(lc.get("source_id"), lc.get("stratum"))] = lc
            for s in strata:
                items = []
                for r in rows:
                    lc = lifecycle_map.get((r.get(id_col), s))
                    if lc and (str(lc.get("truth_status") or "").lower() in {"contradicted", "obsolete", "rejected", "false", "wrong"} or str(lc.get("use_policy") or "").lower() in {"do_not_use", "forbidden", "never_use"}):
                        continue
                    if s == "general":
                        if r.get("status") in ("obsolete", "contradicted"):
                            continue
                        # General only high-ish confidence unless lifecycle confirmed.
                        if float(r.get("confidence") or 0.0) < 0.55 and not (lc and lc.get("truth_status") in ("confirmed", "active")):
                            continue
                    elif s == "recent":
                        if r.get("status") in ("obsolete",):
                            continue
                    else:  # very_recent
                        if not lc or lc.get("stratum") != "very_recent":
                            continue
                    item = dict(r)
                    if lc:
                        item["lifecycle"] = lc
                    items.append(item)
                strata[s][layer] = _compact_rows(items, 80)
        return strata


def synthesize_life_model_patch(current_model: dict[str, Any], delta_evidence: dict[str, Any], *, timeout: float = 180.0) -> tuple[dict[str, Any], str | None]:
    try:
        client = OllamaJsonClient()
        system = (
            "Tu es le Brain2 Life Model Updater. Tu ne réécris PAS toute la vie de William. "
            "Tu proposes uniquement des PATCH OPERATIONS sur le modèle existant: create, confirm, update, weaken, contradict, obsolete ou keep. "
            "Tu dois respecter les strates general/recent/very_recent: general change lentement; recent suit les semaines/mois; very_recent observe les dernières 24-72h. "
            "Aucune psychologie générique, aucune regex, aucune certitude sans preuves. Une occurrence isolée crée au mieux candidate/watch_only, pas une vérité. "
            "Un modèle ne devient proactif live que s'il est actif/confirmé avec preuves, outcomes ou confirmations. JSON strict uniquement."
        )
        prompt = json_dumps({
            "mission": "Update William's canonical life model by patching it, not rebuilding it. Preserve stable knowledge unless new evidence confirms/contradicts it.",
            "current_life_model": current_model,
            "new_delta_evidence": delta_evidence,
            "update_rules": [
                "1 occurrence -> candidate/very_recent/watch_only unless very strong evidence.",
                "2-3 consistent occurrences -> recent hypothesis/active, still cautious.",
                "Repeated confirmations/outcomes -> general/confirmed/proactive_allowed.",
                "Single counter-example weakens only slightly; repeated contradictions can contradict/obsolete.",
                "Never delete/obsolete without counter_evidence and reason.",
                "Separate observed action from inferred need/emotion/intention.",
                "Output patch operations only; include evidence ids/snippets, counter-evidence, lifecycle and live_effect.",
            ],
            "schema": PATCH_SCHEMA,
        })
        return client.require_json(system, prompt, schema_hint=PATCH_SCHEMA, timeout=timeout), None
    except Exception as exc:
        return {"llm_required": True, "error": str(exc)}, str(exc)


def _target_table_for_layer(layer: str) -> tuple[str, str, str] | None:
    return CANONICAL_TABLES.get(layer)


def _minimal_canonical_from_operation(layer: str, op: dict[str, Any]) -> dict[str, Any]:
    """Convert one operation patch_data into the V15.10 canonical schema shape."""
    key = LAYER_TO_CANONICAL_KEY.get(layer)
    if not key:
        return {}
    pdata = op.get("patch_data") if isinstance(op.get("patch_data"), dict) else {}
    identity = op.get("identity_key") or pdata.get("name") or pdata.get("routine_name") or pdata.get("place_key") or pdata.get("hook_name") or "unknown"
    base = dict(pdata)
    base.setdefault("confidence", op.get("confidence_after", op.get("confidence_before", 0.5)))
    base.setdefault("evidence", op.get("evidence") or [])
    base.setdefault("counter_evidence", op.get("counter_evidence") or [])
    # Normalize required name fields per layer.
    if layer == "routine":
        base.setdefault("routine_name", identity)
    elif layer == "place":
        base.setdefault("place_key", identity)
    elif layer == "action_preference":
        base.setdefault("action_or_choice", identity)
    elif layer == "need_expectation":
        base.setdefault("need_or_expectation", identity)
    elif layer == "expression_state":
        base.setdefault("expression_or_style", identity)
    elif layer == "emotional_trajectory":
        base.setdefault("trajectory_name", identity)
    elif layer == "contextual_self":
        base.setdefault("context_key", identity)
    elif layer == "live_prediction_hook":
        base.setdefault("hook_name", identity)
        base.setdefault("horizon", op.get("live_effect", {}).get("horizons", ["H1"])[0] if isinstance(op.get("live_effect"), dict) else "H1")
    elif layer == "affordance_preference":
        base.setdefault("affordance_type", identity)
    return {key: [base]}


def _merge_canonical_models(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k not in out:
            out[k] = []
        if isinstance(out[k], list) and isinstance(v, list):
            out[k].extend(v)
        else:
            out[k] = v
    return out


def apply_life_model_patch(person_id: str, patch_run_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    ensure_life_model_updater_schema()
    now = now_iso()
    ops = _list(patch.get("operations")) if isinstance(patch, dict) else []
    canonical_updates: dict[str, Any] = {}
    applied = 0
    with connect() as con:
        for idx, op in enumerate(ops):
            if not isinstance(op, dict):
                continue
            operation = str(op.get("op") or "keep").lower()
            layer = str(op.get("target_layer") or "unknown").lower()
            stratum = str(op.get("stratum") or "recent").lower()
            if stratum not in {"general", "recent", "very_recent"}:
                stratum = "recent"
            identity_key = str(op.get("identity_key") or op.get("target_id") or f"op_{idx}")[:500]
            table_info = _target_table_for_layer(layer)
            target_table = table_info[0] if table_info else None
            target_id = op.get("target_id")
            # For create/update/confirm, also update canonical tables using V15.10 store function.
            if operation in {"create", "update", "confirm"} and layer in LAYER_TO_CANONICAL_KEY:
                canonical_updates = _merge_canonical_models(canonical_updates, _minimal_canonical_from_operation(layer, op))
            if target_table and not target_id:
                prefix = {
                    "routine": "b2routine", "place": "b2place", "action_preference": "b2actionpref", "need_expectation": "b2need",
                    "expression_state": "b2expr", "emotional_trajectory": "b2traj", "contextual_self": "b2ctxself",
                    "live_prediction_hook": "b2hook", "affordance_preference": "b2affpref",
                }.get(layer, "b2life")
                target_id = stable_id(prefix, person_id, identity_key)
            confidence_before = op.get("confidence_before")
            confidence_after = op.get("confidence_after")
            cd = _clamp(confidence_after, _clamp(confidence_before, 0.5)) - _clamp(confidence_before, 0.5)
            if op.get("confidence_delta") is not None:
                try:
                    cd = float(op.get("confidence_delta"))
                except Exception:
                    pass
            operation_id = stable_id("b2patchop", patch_run_id, idx, operation, layer, identity_key)
            upsert(con, "brain2_life_model_patch_operations", {
                "operation_id": operation_id, "patch_run_id": patch_run_id, "person_id": person_id, "op": operation,
                "target_layer": layer, "target_table": target_table, "target_id": target_id, "identity_key": identity_key,
                "stratum": stratum, "reason": op.get("reason"), "evidence_json": json_dumps(op.get("evidence") or []),
                "counter_evidence_json": json_dumps(op.get("counter_evidence") or []),
                "confidence_before": _clamp(confidence_before) if confidence_before is not None else None,
                "confidence_after": _clamp(confidence_after) if confidence_after is not None else None,
                "confidence_delta": cd, "patch_data_json": json_dumps(op.get("patch_data") or {}),
                "lifecycle_json": json_dumps(op.get("lifecycle") or {}), "live_effect_json": json_dumps(op.get("live_effect") or {}),
                "status": "applied" if operation != "keep" else "recorded", "created_at": now,
            }, "operation_id")
            lifecycle = op.get("lifecycle") if isinstance(op.get("lifecycle"), dict) else {}
            truth_status = lifecycle.get("truth_status") or ({"create": "candidate", "confirm": "confirmed", "update": "active", "weaken": "weakened", "contradict": "contradicted", "obsolete": "obsolete", "keep": "active"}.get(operation, "candidate"))
            use_policy = lifecycle.get("use_policy") or ({"candidate": "watch_only", "weakened": "watch_only", "contradicted": "do_not_use", "obsolete": "do_not_use", "confirmed": "proactive_allowed", "active": "silent_context"}.get(truth_status, "watch_only"))
            if target_table and target_id:
                existing = con.execute("SELECT * FROM brain2_life_model_item_lifecycle WHERE person_id=? AND source_table=? AND source_id=? AND stratum=?", (person_id, target_table, target_id, stratum)).fetchone()
                evidence_count = int(existing["evidence_count"] if existing else 0) + len(_list(op.get("evidence")))
                counter_count = int(existing["counter_evidence_count"] if existing else 0) + len(_list(op.get("counter_evidence")))
                upsert(con, "brain2_life_model_item_lifecycle", {
                    "lifecycle_id": stable_id("b2lifecycle", person_id, target_table, target_id, stratum),
                    "person_id": person_id, "source_table": target_table, "source_id": target_id, "layer": layer,
                    "identity_key": identity_key, "stratum": stratum, "truth_status": truth_status,
                    "first_seen_at": existing["first_seen_at"] if existing else now,
                    "last_seen_at": now,
                    "last_confirmed_at": now if truth_status in {"active", "confirmed"} else (existing["last_confirmed_at"] if existing else None),
                    "last_contradicted_at": now if truth_status in {"contradicted", "weakened", "obsolete"} else (existing["last_contradicted_at"] if existing else None),
                    "evidence_count": evidence_count, "counter_evidence_count": counter_count,
                    "confidence": _clamp(confidence_after, _clamp(confidence_before, 0.5)),
                    "recency_weight": 1.0 if stratum == "very_recent" else (0.75 if stratum == "recent" else 0.45),
                    "staleness_score": 0.0 if truth_status not in {"obsolete", "contradicted"} else 1.0,
                    "valid_from": lifecycle.get("valid_from"), "valid_until": lifecycle.get("valid_until"),
                    "superseded_by": lifecycle.get("superseded_by"), "obsolete_reason": lifecycle.get("obsolete_reason"),
                    "use_policy": use_policy, "notes_json": json_dumps({"reason": op.get("reason"), "live_effect": op.get("live_effect") or {}}),
                    "updated_at": now, "created_at": existing["created_at"] if existing else now,
                }, "lifecycle_id")
            applied += 1
        con.commit()
    if canonical_updates:
        store_canonical_life_model(person_id, patch_run_id, canonical_updates)
    update_life_model_strata(person_id, patch_run_id=patch_run_id)
    return {"applied_operations": applied, "canonical_update_layers": list(canonical_updates.keys())}


def update_life_model_strata(person_id: str, *, patch_run_id: str | None = None) -> dict[str, Any]:
    ensure_life_model_updater_schema()
    now = now_iso()
    strata = _summarize_strata(person_id)
    with connect() as con:
        for stratum, model in strata.items():
            if stratum == "very_recent":
                start = _iso(_now_dt() - timedelta(days=3))
            elif stratum == "recent":
                start = _iso(_now_dt() - timedelta(days=45))
            else:
                start = None
            upsert(con, "brain2_life_model_strata", {
                "stratum_id": stable_id("b2stratum", person_id, stratum), "person_id": person_id, "stratum": stratum,
                "status": "active", "model_json": json_dumps(model), "evidence_window_start": start,
                "evidence_window_end": now, "patch_run_id": patch_run_id, "source_counts_json": json_dumps(_count(model)),
                "created_at": now, "updated_at": now,
            }, "stratum_id")
        con.commit()
    return {"person_id": person_id, "strata": {k: _count(v) for k, v in strata.items()}}


def run_brain2_life_model_update(person_id: str, *, period_start: str | None = None, period_end: str | None = None, use_llm: bool = True, timeout: float = 180.0, limit: int = 120, bootstrap_if_empty: bool = True) -> dict[str, Any]:
    """Patch the life model with delta evidence and keep general/recent/very_recent snapshots."""
    ensure_life_model_updater_schema()
    now = now_iso()
    current = load_current_life_model(person_id, limit=limit)
    has_current = any(current.get("canonical_layers", {}).get(layer) for layer in current.get("canonical_layers", {}))
    if bootstrap_if_empty and not has_current:
        # First run: V15.10 builds the first canonical base from Brain2 evidence.
        bootstrap = build_brain2_canonical_life_model(person_id, period_start=period_start, period_end=period_end, use_llm=use_llm, timeout=timeout, limit=limit)
        update_life_model_strata(person_id, patch_run_id=bootstrap.get("export_id"))
        return {"version": VERSION, "person_id": person_id, "mode": "bootstrap_v15_10", "bootstrap": bootstrap, "strata": update_life_model_strata(person_id, patch_run_id=bootstrap.get("export_id"))}
    delta = collect_life_model_delta(person_id, period_start=period_start, period_end=period_end, limit=limit)
    patch_run_id = stable_id("b2patch", person_id, period_start or "delta", period_end or now, now)
    error: str | None = None
    patch: dict[str, Any]
    if use_llm:
        patch, error = synthesize_life_model_patch(current, delta, timeout=timeout)
        status = "llm_patch_ready" if not error else "delta_ready_llm_required"
    else:
        patch = {"llm_required": True, "reason": "use_llm=false", "operations": []}
        status = "delta_only_llm_disabled"
    with connect() as con:
        upsert(con, "brain2_life_model_patch_runs", {
            "patch_run_id": patch_run_id, "person_id": person_id, "status": status,
            "period_start": period_start, "period_end": period_end,
            "current_model_digest": stable_id("digest", json_dumps(_count(current))),
            "delta_counts_json": json_dumps(_count(delta)), "patch_json": json_dumps(patch), "error_text": error,
            "llm_model": None, "created_at": now, "finished_at": now_iso(),
        }, "patch_run_id")
        con.commit()
    applied = {"applied_operations": 0}
    if isinstance(patch, dict) and not patch.get("llm_required"):
        applied = apply_life_model_patch(person_id, patch_run_id, patch)
    else:
        update_life_model_strata(person_id, patch_run_id=patch_run_id)
    return {"version": VERSION, "person_id": person_id, "patch_run_id": patch_run_id, "status": status, "delta_counts": _count(delta), "patch": patch, "applied": applied}


def latest_life_model_strata(person_id: str) -> dict[str, Any]:
    ensure_life_model_updater_schema()
    with connect() as con:
        rows = _query(con, "SELECT * FROM brain2_life_model_strata WHERE person_id=? ORDER BY updated_at DESC", (person_id,))
    return {r.get("stratum") or "unknown": {"status": r.get("status"), "model": _safe_json(r.get("model_json"), {}), "updated_at": r.get("updated_at"), "source_counts": _safe_json(r.get("source_counts_json"), {})} for r in rows}


def brain2_life_model_update_audit(person_id: str) -> dict[str, Any]:
    ensure_life_model_updater_schema()
    strata = latest_life_model_strata(person_id)
    with connect() as con:
        runs = _query(con, "SELECT patch_run_id,status,period_start,period_end,delta_counts_json,created_at FROM brain2_life_model_patch_runs WHERE person_id=? ORDER BY created_at DESC LIMIT 5", (person_id,)) if _table_exists(con, "brain2_life_model_patch_runs") else []
        ops = _query(con, "SELECT op,target_layer,stratum,truth_status,use_policy,COUNT(*) AS c FROM brain2_life_model_patch_operations po LEFT JOIN brain2_life_model_item_lifecycle lc ON lc.person_id=po.person_id AND lc.source_id=po.target_id AND lc.stratum=po.stratum WHERE po.person_id=? GROUP BY op,target_layer,stratum,truth_status,use_policy", (person_id,)) if _table_exists(con, "brain2_life_model_patch_operations") else []
    return {"version": VERSION, "person_id": person_id, "strata_available": list(strata.keys()), "strata_counts": {k: v.get("source_counts", {}) for k, v in strata.items()}, "recent_patch_runs": runs, "operation_counts": ops, "verdict": "ready" if strata else "needs_update"}

# Preserve the legacy writer under explicit names before replacing public entry
# points with V18's validation, owner scope and lifecycle gates.
_v17_apply_life_model_patch = apply_life_model_patch
_v17_run_brain2_life_model_update = run_brain2_life_model_update
from . import brain2_life_model_v15_10 as _v18_canonical_life_model_module
from .v18_life_model import install_updater as _install_v18_life_model_updater
_globals_v18_life_model_updater = _install_v18_life_model_updater(__import__(__name__, fromlist=['*']), _v18_canonical_life_model_module)
globals().update(_globals_v18_life_model_updater)
