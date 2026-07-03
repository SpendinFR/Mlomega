from __future__ import annotations

"""V16.0 non-verbal life event bridge.

BrainLive is fast during the day and can observe scenes where there is little or
no speech: computer work, cigarette/pause, walking, resting, waiting, room/desk
state, visible affordances.  V13/V14 were historically conversation-first, so a
silent scene could remain only as a vision/context row and never become a proper
Brain2 episode/life event.

This module runs offline after V15.14 event assembly. It does not reconstruct a
conversation and it does not inject BrainLive predictions as dialogue. It creates
explicit non-verbal candidates and, when useful, materializes them as observed
life_events + memory_cards with exact raw evidence and cautious hypotheses.

The LLM/VLM is optional and offline only. It is used to classify a silent bundle
into a candidate activity/need/mood-effect hypothesis; never to invent facts.
"""

from typing import Any

from .db import connect, init_db, upsert
from .life_memory import add_life_event
from .memory_foundation import TRUTH_INFERRED, TRUTH_OBSERVED, add_memory_facet, add_memory_link
from .utils import json_dumps, json_loads, now_iso, stable_id

VERSION = "16.0.0-silent-nonverbal-life-events"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_silent_event_candidates_v160(
  candidate_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  bundle_id TEXT NOT NULL,
  conversation_id TEXT,
  live_session_id TEXT,
  start_time TEXT,
  end_time TEXT,
  place_json TEXT DEFAULT '{}',
  transcript_chars INTEGER DEFAULT 0,
  vision_evidence_json TEXT DEFAULT '[]',
  deep_vision_evidence_json TEXT DEFAULT '[]',
  world_evidence_json TEXT DEFAULT '[]',
  audio_evidence_json TEXT DEFAULT '[]',
  activity_candidates_json TEXT DEFAULT '[]',
  inferred_activity_type TEXT,
  title TEXT,
  summary TEXT,
  likely_need_hypothesis TEXT,
  mood_effect_hypothesis TEXT,
  routine_signal_json TEXT DEFAULT '{}',
  exact_evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  memory_action TEXT DEFAULT 'watch',
  use_policy TEXT DEFAULT 'silent_context',
  status TEXT DEFAULT 'candidate',
  llm_json TEXT DEFAULT '{}',
  created_life_event_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_silent_life_mining_runs_v160(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  scanned_bundles INTEGER DEFAULT 0,
  silent_candidates INTEGER DEFAULT 0,
  exported_life_events INTEGER DEFAULT 0,
  status TEXT NOT NULL,
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blsilent160_person_date ON brainlive_silent_event_candidates_v160(person_id, package_date, start_time);
CREATE INDEX IF NOT EXISTS idx_blsilent160_bundle ON brainlive_silent_event_candidates_v160(bundle_id, status);
"""

SILENT_SCHEMA_HINT: dict[str, Any] = {
    "memory_action": "store|watch|ignore",
    "inferred_activity_type": "computer_work|smoking_pause|walking|resting|waiting|social_presence|travel|household|unknown",
    "title": "short title",
    "summary": "what was visibly happening, not inner certainty",
    "likely_need_hypothesis": "optional cautious hypothesis",
    "mood_effect_hypothesis": "optional cautious hypothesis about before/after state",
    "routine_signal": {"temporal_pattern": "", "place_pattern": "", "affordances": [], "repeat_watch": True},
    "exact_evidence": ["exact snippets from provided vision/world/audio evidence"],
    "counter_evidence": ["missing/contradicting evidence"],
    "confidence": 0.0,
    "use_policy": "silent_context|watch_only|routine_candidate|proactive_allowed",
}


def ensure_silent_life_schema() -> None:
    init_db()
    with connect() as con:
        con.executescript(SCHEMA)
        cols = {str(r[1]) for r in con.execute("PRAGMA table_info(brainlive_silent_event_candidates_v160)").fetchall()}
        if "deep_vision_evidence_json" not in cols:
            con.execute("ALTER TABLE brainlive_silent_event_candidates_v160 ADD COLUMN deep_vision_evidence_json TEXT DEFAULT '[]'")
        con.commit()


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _rows(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _safe_json(v: Any, default: Any) -> Any:
    if isinstance(v, (dict, list)):
        return v
    return json_loads(v if isinstance(v, str) else None, default)


def _clip(text: Any, n: int = 1200) -> str:
    s = str(text or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _transcript_chars(bundle: dict[str, Any]) -> int:
    return sum(len(str(t.get("text") or "")) for t in (_safe_json(bundle.get("transcript_json"), []) or []))


def _evidence_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    vision = _safe_json(bundle.get("vision_timeline_json"), []) or []
    world = _safe_json(bundle.get("world_state_timeline_json"), []) or []
    audio = _safe_json(bundle.get("audio_timeline_json"), []) or []
    place = _safe_json(bundle.get("place_json"), {}) or {}
    vision_ev: list[dict[str, Any]] = []
    deep_vision_ev: list[dict[str, Any]] = []
    activity_candidates: list[str] = []
    objects: list[str] = []
    affordances: list[str] = []

    def add_unique(bucket: list[str], value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("activity") or value.get("label") or value.get("type") or value.get("name") or json_dumps(value)
        if value and str(value) not in bucket:
            bucket.append(str(value))

    # V16.1: prefer offline deep VLM observations when available. These are
    # detailed image analyses run after live stop, not live-loop shortcuts.
    try:
        with connect() as con:
            if _table_exists(con, "brainlive_deep_vision_observations_v161"):
                rows = _rows(con, """
                    SELECT * FROM brainlive_deep_vision_observations_v161
                    WHERE bundle_id=? AND status IN ('ok','vlm_error','skipped_no_vlm')
                    ORDER BY sample_index, frame_time
                    LIMIT 30
                """, (bundle.get("bundle_id"),))
            else:
                rows = []
    except Exception:
        rows = []
    for d in rows:
        objs = _safe_json(d.get("objects_json"), []) or []
        affs = _safe_json(d.get("affordances_json"), []) or []
        screens = _safe_json(d.get("screens_or_devices_json"), []) or []
        exact = _safe_json(d.get("exact_visual_evidence_json"), []) or []
        if d.get("observed_activity"):
            add_unique(activity_candidates, d.get("observed_activity"))
        for x in objs:
            add_unique(objects, x)
        for x in affs:
            add_unique(affordances, x)
        txt = " | ".join([p for p in [
            _clip(d.get("scene_summary_detailed"), 1200),
            f"activité_visible={d.get('observed_activity')}" if d.get("observed_activity") else "",
            f"lieu={d.get('location_hint')}" if d.get("location_hint") else "",
            f"spatial={d.get('spatial_layout')}" if d.get("spatial_layout") else "",
            f"objets={json_dumps(objs)}" if objs else "",
            f"affordances={json_dumps(affs)}" if affs else "",
            f"ecrans_appareils={json_dumps(screens)}" if screens else "",
            f"preuves_visuelles={json_dumps(exact[:6])}" if exact else "",
        ] if p])
        if txt:
            deep_vision_ev.append({"time": d.get("frame_time"), "text": txt, "frame_id": d.get("frame_id"), "image_path": d.get("image_path"), "source_id": d.get("deep_observation_id"), "model": d.get("model"), "status": d.get("status")})

    for v in vision[:30]:
        acts = v.get("possible_user_activities") or []
        if isinstance(acts, str):
            acts = [acts]
        objs = v.get("objects") or []
        affs = v.get("affordances") or []
        for x in acts or []:
            add_unique(activity_candidates, x)
        for x in objs or []:
            add_unique(objects, x)
        for x in affs or []:
            add_unique(affordances, x)
        txt = " | ".join([p for p in [
            _clip(v.get("summary"), 700),
            f"lieu={v.get('location_hint')}" if v.get("location_hint") else "",
            f"spatial={v.get('spatial_context')}" if v.get("spatial_context") else "",
            f"activites_possibles={json_dumps(acts)}" if acts else "",
            f"objets={json_dumps(objs)}" if objs else "",
            f"affordances={json_dumps(affs)}" if affs else "",
        ] if p])
        if txt:
            vision_ev.append({"time": v.get("time"), "text": txt, "frame_id": v.get("frame_id"), "image_path": v.get("image_path"), "source_id": v.get("source_id")})
    world_ev: list[dict[str, Any]] = []
    for w in world[:20]:
        payload = w.get("payload") or {}
        txt = _clip(w.get("summary") or payload.get("where_am_i") or payload.get("what_is_happening") or payload.get("active_location_hint") or json_dumps({k: payload.get(k) for k in ("where_am_i", "active_location_hint", "people_present_json", "topic_hint") if payload.get(k) is not None}), 900)
        if txt:
            world_ev.append({"time": w.get("time"), "text": txt, "source_id": w.get("source_id")})
    audio_ev: list[dict[str, Any]] = []
    for a in audio[:20]:
        txt = _clip(a.get("summary"), 700)
        if txt:
            audio_ev.append({"time": a.get("time"), "text": txt, "kind": a.get("kind"), "source_id": a.get("source_id")})
    return {"vision": vision_ev, "deep_vision": deep_vision_ev, "world": world_ev, "audio": audio_ev, "place": place, "activity_candidates": activity_candidates, "objects": objects, "affordances": affordances}


def _fallback_candidate(bundle: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    acts = evidence.get("activity_candidates") or []
    affs = evidence.get("affordances") or []
    objs = evidence.get("objects") or []
    place = evidence.get("place") or {}
    activity = str(acts[0]) if acts else "unknown_nonverbal_activity"
    summary_bits = []
    if acts:
        summary_bits.append("activités possibles: " + ", ".join(acts[:4]))
    if place.get("dominant_hint"):
        summary_bits.append("lieu: " + str(place.get("dominant_hint")))
    if objs:
        summary_bits.append("objets visibles: " + ", ".join(objs[:8]))
    if affs:
        summary_bits.append("affordances: " + ", ".join(affs[:8]))
    exact = [x["text"] for x in (evidence.get("deep_vision") or [])[:8]] + [x["text"] for x in (evidence.get("vision") or [])[:6]] + [x["text"] for x in (evidence.get("world") or [])[:4]]
    conf = 0.45 + min(0.25, 0.04 * len(exact))
    return {
        "memory_action": "watch" if acts or exact else "ignore",
        "inferred_activity_type": activity,
        "title": "Épisode non verbal observé",
        "summary": "; ".join(summary_bits) or "Épisode non verbal avec contexte visuel/lieu, sans parole exploitable.",
        "likely_need_hypothesis": None,
        "mood_effect_hypothesis": None,
        "routine_signal": {"temporal_pattern": None, "place_pattern": place.get("dominant_hint"), "affordances": affs[:10], "repeat_watch": True},
        "exact_evidence": exact[:10],
        "counter_evidence": ["pas ou peu de transcription humaine dans ce bundle"],
        "confidence": min(0.70, conf),
        "use_policy": "watch_only",
    }


def _llm_candidate(bundle: dict[str, Any], evidence: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
    from .llm import OllamaJsonClient
    system = (
        "Tu es Brain2 Nonverbal Life Event Miner. Tu analyses un événement SANS ou avec très peu de parole. "
        "Tu dois décrire ce qui est observé via vision/lieu/audio léger, puis proposer seulement des hypothèses prudentes. "
        "Ne transforme jamais une activité visible en vérité psychologique. Sépare action observée, besoin hypothétique, effet humeur hypothétique. "
        "Si les preuves sont faibles: memory_action=watch ou ignore. Si les preuves sont répétables/utiles: store. "
        "Chaque hypothèse doit citer des exact_evidence fournis dans l'entrée. JSON strict uniquement."
    )
    prompt = json_dumps({
        "bundle": {k: bundle.get(k) for k in ("bundle_id", "start_time", "end_time", "title", "bundle_kind", "live_session_id", "brain2_conversation_id")},
        "transcript_chars": _transcript_chars(bundle),
        "place": evidence.get("place"),
        "deep_vision_evidence": evidence.get("deep_vision"),
        "vision_evidence": evidence.get("vision"),
        "world_evidence": evidence.get("world"),
        "audio_evidence": evidence.get("audio"),
        "activity_candidates_from_vlm": evidence.get("activity_candidates"),
        "objects": evidence.get("objects"),
        "affordances": evidence.get("affordances"),
        "contract": [
            "Pas de reconstruction de conversation.",
            "Pas de certitude sur les intentions ou humeur.",
            "Action visible != besoin interne.",
            "Une seule occurrence devient watch_only/candidate sauf preuve très forte.",
            "Citer exact_evidence depuis les textes fournis."
        ],
    })
    out = OllamaJsonClient().require_json(system, prompt, schema_hint=SILENT_SCHEMA_HINT, timeout=timeout)
    return out if isinstance(out, dict) else {}


def _normalize_candidate(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    out = {**fallback, **{k: v for k, v in (raw or {}).items() if v is not None}}
    action = str(out.get("memory_action") or "watch").strip().lower()
    if action not in {"store", "watch", "ignore"}:
        action = "watch"
    policy = str(out.get("use_policy") or "silent_context").strip().lower()
    if policy not in {"silent_context", "watch_only", "routine_candidate", "proactive_allowed"}:
        policy = "silent_context"
    if action != "store" and policy == "proactive_allowed":
        policy = "watch_only"
    try:
        conf = float(out.get("confidence") if out.get("confidence") is not None else fallback.get("confidence") or 0.5)
    except Exception:
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    exact = out.get("exact_evidence") or fallback.get("exact_evidence") or []
    if isinstance(exact, str):
        exact = [exact]
    counter = out.get("counter_evidence") or []
    if isinstance(counter, str):
        counter = [counter]
    routine = out.get("routine_signal") or fallback.get("routine_signal") or {}
    if not isinstance(routine, dict):
        routine = {"raw": routine}
    return {
        "memory_action": action,
        "inferred_activity_type": str(out.get("inferred_activity_type") or fallback.get("inferred_activity_type") or "unknown_nonverbal_activity")[:120],
        "title": _clip(out.get("title") or fallback.get("title") or "Épisode non verbal", 240),
        "summary": _clip(out.get("summary") or fallback.get("summary") or "Épisode non verbal observé.", 2000),
        "likely_need_hypothesis": _clip(out.get("likely_need_hypothesis"), 1000) if out.get("likely_need_hypothesis") else None,
        "mood_effect_hypothesis": _clip(out.get("mood_effect_hypothesis"), 1000) if out.get("mood_effect_hypothesis") else None,
        "routine_signal": routine,
        "exact_evidence": [str(x)[:1200] for x in exact if x][:12],
        "counter_evidence": [str(x)[:1200] for x in counter if x][:8],
        "confidence": conf,
        "use_policy": policy,
        "llm_json": raw or {},
    }


def _event_type(activity: str) -> str:
    a = (activity or "").lower()
    if any(x in a for x in ["work", "computer", "ordinateur", "desk", "projet"]):
        return "work"
    if any(x in a for x in ["smok", "cigarette", "pause", "break"]):
        return "health"
    if any(x in a for x in ["walk", "walking", "marche", "sort", "outside"]):
        return "location"
    if any(x in a for x in ["rest", "relax", "détente", "attente", "waiting"]):
        return "health"
    return "action"


def mine_silent_nonverbal_life_events(
    person_id: str = "me",
    *,
    package_date: str | None = None,
    use_llm: bool = True,
    timeout: float = 120.0,
    transcript_char_threshold: int = 80,
    export_life_events: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Create cautious Brain2 life events from silent/low-speech BrainLive bundles.

    This is an offline post-stop step. It scans V15.14 bundles, keeps bundles with
    little human speech but usable vision/world/audio evidence, and turns them
    into candidate non-verbal life events with exact evidence.
    """
    ensure_silent_life_schema()
    from .brainlive_event_assembler_v15_14 import _period_bounds
    day = _period_bounds(package_date)[0]
    run_id = stable_id("blsilent160run", person_id, day, now_iso())
    now = now_iso()
    scanned = 0
    candidates = 0
    exported = 0
    status = "ok"
    error = None
    try:
        with connect() as con:
            if not _table_exists(con, "brainlive_event_bundles_v1514"):
                bundles = []
            else:
                bundles = _rows(con, "SELECT * FROM brainlive_event_bundles_v1514 WHERE person_id=? AND package_date=? ORDER BY start_time LIMIT ?", (person_id, day, limit))
            scanned = len(bundles)
            for b in bundles:
                chars = _transcript_chars(b)
                evidence = _evidence_from_bundle(b)
                has_nonverbal = bool(evidence.get("vision") or evidence.get("world") or evidence.get("audio"))
                if chars > transcript_char_threshold or not has_nonverbal:
                    continue
                fallback = _fallback_candidate(b, evidence)
                raw_llm: dict[str, Any] = {}
                if use_llm:
                    raw_llm = _llm_candidate(b, evidence, timeout=timeout)
                cand = _normalize_candidate(raw_llm, fallback)
                if cand["memory_action"] == "ignore" and not cand.get("exact_evidence"):
                    continue
                candidate_id = stable_id("blsilent160", person_id, b.get("bundle_id"), cand.get("summary"), cand.get("inferred_activity_type"))
                life_event_id: str | None = None
                if export_life_events and cand["memory_action"] in {"store", "watch"} and cand["confidence"] >= 0.35:
                    location_text = None
                    place = evidence.get("place") or {}
                    if isinstance(place, dict):
                        location_text = place.get("dominant_hint") or ", ".join(place.get("all_hints") or []) or None
                    objects = [str(x) for x in (evidence.get("objects") or [])[:20]]
                    metadata = {
                        "source": "brainlive_silent_event_v16_0",
                        "bundle_id": b.get("bundle_id"),
                        "candidate_id": candidate_id,
                        "use_policy": cand.get("use_policy"),
                        "memory_action": cand.get("memory_action"),
                        "routine_signal": cand.get("routine_signal"),
                        "likely_need_hypothesis": cand.get("likely_need_hypothesis"),
                        "mood_effect_hypothesis": cand.get("mood_effect_hypothesis"),
                        "not_user_speech": True,
                    }
                    conv_id = b.get("brain2_conversation_id")
                    if conv_id and not con.execute("SELECT 1 FROM conversations WHERE conversation_id=?", (conv_id,)).fetchone():
                        conv_id = None
                    life_event_id = add_life_event(
                        con,
                        event_type=_event_type(cand["inferred_activity_type"]),
                        title=cand["title"],
                        summary=cand["summary"],
                        subject_person_id=person_id,
                        event_status="observed_nonverbal" if cand["memory_action"] == "store" else "candidate_nonverbal_observation",
                        truth_status=TRUTH_OBSERVED if cand["memory_action"] == "store" and cand["confidence"] >= 0.65 else TRUTH_INFERRED,
                        life_domain="daily_life",
                        topic=cand["inferred_activity_type"],
                        location_text=location_text,
                        people=[],
                        objects=objects,
                        emotional_valence=None,
                        temporal_status="observed_past",
                        occurred_start=b.get("start_time"),
                        occurred_end=b.get("end_time"),
                        importance_score=max(0.35, min(0.75, cand["confidence"])),
                        confidence=cand["confidence"],
                        source_conversation_id=conv_id,
                        evidence_text="\n".join(cand.get("exact_evidence") or [])[:4000],
                        metadata=metadata,
                    )
                    add_memory_facet(con, target_table="life_events", target_id=life_event_id, facet_type="nonverbal_activity_type", facet_value=cand["inferred_activity_type"], source="brainlive_silent_v16", confidence=cand["confidence"])
                    add_memory_facet(con, target_table="life_events", target_id=life_event_id, facet_type="use_policy", facet_value=cand["use_policy"], source="brainlive_silent_v16", confidence=cand["confidence"])
                    add_memory_link(con, from_table="brainlive_event_bundles_v1514", from_id=str(b.get("bundle_id")), relation_type="materializes_nonverbal_life_event", to_table="life_events", to_id=life_event_id, confidence=cand["confidence"], metadata={"candidate_id": candidate_id})
                    exported += 1
                upsert(con, "brainlive_silent_event_candidates_v160", {
                    "candidate_id": candidate_id,
                    "person_id": person_id,
                    "package_date": day,
                    "bundle_id": b.get("bundle_id"),
                    "conversation_id": b.get("brain2_conversation_id"),
                    "live_session_id": b.get("live_session_id"),
                    "start_time": b.get("start_time"),
                    "end_time": b.get("end_time"),
                    "place_json": json_dumps(evidence.get("place") or {}),
                    "transcript_chars": chars,
                    "vision_evidence_json": json_dumps(evidence.get("vision") or []),
                    "deep_vision_evidence_json": json_dumps(evidence.get("deep_vision") or []),
                    "world_evidence_json": json_dumps(evidence.get("world") or []),
                    "audio_evidence_json": json_dumps(evidence.get("audio") or []),
                    "activity_candidates_json": json_dumps(evidence.get("activity_candidates") or []),
                    "inferred_activity_type": cand["inferred_activity_type"],
                    "title": cand["title"],
                    "summary": cand["summary"],
                    "likely_need_hypothesis": cand.get("likely_need_hypothesis"),
                    "mood_effect_hypothesis": cand.get("mood_effect_hypothesis"),
                    "routine_signal_json": json_dumps(cand.get("routine_signal") or {}),
                    "exact_evidence_json": json_dumps(cand.get("exact_evidence") or []),
                    "counter_evidence_json": json_dumps(cand.get("counter_evidence") or []),
                    "confidence": cand["confidence"],
                    "memory_action": cand["memory_action"],
                    "use_policy": cand["use_policy"],
                    "status": "exported" if life_event_id else "candidate",
                    "llm_json": json_dumps(cand.get("llm_json") or {}),
                    "created_life_event_id": life_event_id,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                }, "candidate_id")
                candidates += 1
            upsert(con, "brainlive_silent_life_mining_runs_v160", {
                "run_id": run_id,
                "person_id": person_id,
                "package_date": day,
                "scanned_bundles": scanned,
                "silent_candidates": candidates,
                "exported_life_events": exported,
                "status": status,
                "error_text": error,
                "created_at": now,
                "updated_at": now_iso(),
            }, "run_id")
            con.commit()
    except Exception as exc:
        status = "error"
        error = str(exc)[:2000]
        with connect() as con:
            upsert(con, "brainlive_silent_life_mining_runs_v160", {
                "run_id": run_id,
                "person_id": person_id,
                "package_date": day,
                "scanned_bundles": scanned,
                "silent_candidates": candidates,
                "exported_life_events": exported,
                "status": status,
                "error_text": error,
                "created_at": now,
                "updated_at": now_iso(),
            }, "run_id")
            con.commit()
        raise
    return {"version": VERSION, "run_id": run_id, "person_id": person_id, "package_date": day, "scanned_bundles": scanned, "silent_candidates": candidates, "exported_life_events": exported, "status": status}


def silent_life_audit(person_id: str = "me", *, package_date: str | None = None) -> dict[str, Any]:
    ensure_silent_life_schema()
    from .brainlive_event_assembler_v15_14 import _period_bounds
    day = _period_bounds(package_date)[0]
    with connect() as con:
        candidates = _rows(con, "SELECT status, memory_action, use_policy, COUNT(*) AS n FROM brainlive_silent_event_candidates_v160 WHERE person_id=? AND package_date=? GROUP BY status, memory_action, use_policy", (person_id, day))
        latest = con.execute("SELECT * FROM brainlive_silent_life_mining_runs_v160 WHERE person_id=? AND package_date=? ORDER BY created_at DESC LIMIT 1", (person_id, day)).fetchone()
    return {"version": VERSION, "person_id": person_id, "package_date": day, "candidate_counts": candidates, "latest_run": dict(latest) if latest else None}

# V18: non-verbal inference remains a scoped candidate until independently promoted.
from .v18_poststop_outputs import install_silent as _install_v18_silent_outputs
_globals_v18_silent_outputs = _install_v18_silent_outputs(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_silent_outputs)
