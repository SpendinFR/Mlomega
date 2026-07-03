from __future__ import annotations

"""V17 longitudinal observed-case and global pattern engine.

This module is deliberately not a new psychological oracle.  It turns the
already-analysed Brain2 material (episodes, situations, states, choices,
outcomes, relationships, speech acts, vision/world metadata) into comparable
observed life cases, then mines repeated structures across the whole personal
history.  The point is mechanical longitudinal memory:

    episode -> observed_case -> empirical prediction_case -> similarity edges
    -> global patterns with evidence and counterexamples -> day/week/month runs.

LLM reasoning may later enrich labels, but the core comparison/counting here is
source-grounded and deterministic so the system can say why a pattern exists.
"""

import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .db import connect, init_db, upsert
from .utils import json_dumps, json_loads, now_iso, stable_id

VERSION = "17.1.0-longitudinal-observed-cases"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brain2_observed_cases_v17(
  observed_case_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  conversation_id TEXT,
  episode_id TEXT,
  case_type TEXT NOT NULL,
  case_key TEXT NOT NULL,
  title TEXT NOT NULL,
  context_summary TEXT NOT NULL,
  trigger_summary TEXT,
  activity_type TEXT,
  place_text TEXT,
  people_json TEXT DEFAULT '[]',
  relation_context_json TEXT DEFAULT '{}',
  state_before_json TEXT DEFAULT '{}',
  state_after_json TEXT DEFAULT '{}',
  emotion_before TEXT,
  emotion_after TEXT,
  action_summary TEXT,
  choice_summary TEXT,
  outcome_summary TEXT,
  duration_s REAL,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  tags_json TEXT DEFAULT '[]',
  comparable_vector_json TEXT DEFAULT '{}',
  embedding_text TEXT,
  quality_score REAL DEFAULT 0.6,
  confidence REAL DEFAULT 0.6,
  observed_at TEXT,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_b2_obs_case_episode_unique ON brain2_observed_cases_v17(person_id, episode_id);
CREATE INDEX IF NOT EXISTS idx_b2_obs_case_person_time ON brain2_observed_cases_v17(person_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_b2_obs_case_key ON brain2_observed_cases_v17(person_id, case_key, status);

CREATE TABLE IF NOT EXISTS brain2_case_similarity_edges_v17(
  edge_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  anchor_case_id TEXT NOT NULL,
  similar_case_id TEXT NOT NULL,
  final_score REAL NOT NULL,
  semantic_similarity REAL DEFAULT 0,
  situation_similarity REAL DEFAULT 0,
  state_similarity REAL DEFAULT 0,
  relationship_similarity REAL DEFAULT 0,
  outcome_similarity REAL DEFAULT 0,
  language_similarity REAL DEFAULT 0,
  shared_features_json TEXT DEFAULT '{}',
  differences_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_b2_case_sim_unique ON brain2_case_similarity_edges_v17(person_id, anchor_case_id, similar_case_id);
CREATE INDEX IF NOT EXISTS idx_b2_case_sim_anchor ON brain2_case_similarity_edges_v17(person_id, anchor_case_id, final_score);

CREATE TABLE IF NOT EXISTS brain2_global_life_patterns_v17(
  pattern_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  pattern_type TEXT NOT NULL,
  pattern_key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  recurrence_count INTEGER DEFAULT 0,
  context_count INTEGER DEFAULT 0,
  people_count INTEGER DEFAULT 0,
  counterexample_count INTEGER DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT,
  evidence_case_ids_json TEXT DEFAULT '[]',
  counterexample_case_ids_json TEXT DEFAULT '[]',
  contexts_json TEXT DEFAULT '[]',
  people_json TEXT DEFAULT '[]',
  usual_trigger TEXT,
  usual_state_before TEXT,
  usual_action TEXT,
  usual_outcome TEXT,
  hidden_loop_hypothesis TEXT,
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'candidate',
  stratum TEXT DEFAULT 'recent',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(person_id, pattern_key)
);

CREATE TABLE IF NOT EXISTS brain2_longitudinal_runs_v17(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  period TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  status TEXT NOT NULL,
  conversations_processed INTEGER DEFAULT 0,
  cases_built INTEGER DEFAULT 0,
  similarity_edges INTEGER DEFAULT 0,
  patterns_upserted INTEGER DEFAULT 0,
  results_json TEXT DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

_BAD_STATUSES = {"deleted", "obsolete", "contradicted", "disabled", "archived"}
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "dans", "avec", "pour", "une", "des", "les", "que",
    "qui", "est", "pas", "plus", "sur", "son", "ses", "mon", "mes", "william", "context", "summary",
}


def ensure_longitudinal_case_schema() -> None:
    init_db()
    with connect() as con:
        con.executescript(SCHEMA)
        con.commit()


def _rows(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    try:
        r = con.execute(sql, params).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _cols(con, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if s.startswith("["):
            data = json_loads(s, [])
            return data if isinstance(data, list) else [data]
        return [s]
    return [value]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        data = json_loads(value, {})
        return data if isinstance(data, dict) else {}
    return {}


def _txt(*parts: Any) -> str:
    return " ".join(str(p).strip() for p in parts if p is not None and str(p).strip())


def _norm_tokenize(text: str) -> set[str]:
    out: set[str] = set()
    cur = []
    for ch in (text or "").lower():
        if ch.isalnum() or ch in "_-":
            cur.append(ch)
        else:
            if len(cur) >= 3:
                token = "".join(cur)
                if token not in _STOPWORDS:
                    out.add(token[:40])
            cur = []
    if len(cur) >= 3:
        token = "".join(cur)
        if token not in _STOPWORDS:
            out.add(token[:40])
    return out


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _iso_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def period_bounds(period: str = "day", *, run_date: str | None = None, timezone_name: str | None = None, period_start: str | None = None, period_end: str | None = None) -> tuple[str | None, str | None, str]:
    if period_start or period_end:
        return period_start, period_end, f"{period}:{period_start or '...'}->{period_end or '...'}"
    tz_name = timezone_name or os.environ.get("MLOMEGA_LOCAL_TZ") or "Europe/Paris"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    base = datetime.now(tz)
    if run_date:
        try:
            base = datetime.fromisoformat(run_date[:10]).replace(tzinfo=tz)
        except Exception:
            pass
    start_local = base.replace(hour=0, minute=0, second=0, microsecond=0)
    p = (period or "day").lower()
    if p in {"hour", "daytime"}:
        end_local = base.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        start_local = end_local - timedelta(hours=1)
    elif p == "day":
        end_local = start_local + timedelta(days=1)
    elif p == "week":
        start_local = (base - timedelta(days=base.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=7)
    elif p == "month":
        start_local = base.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_local.month == 12:
            end_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            end_local = start_local.replace(month=start_local.month + 1)
    elif p in {"quarter", "year"}:
        if p == "year":
            start_local = base.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end_local = start_local.replace(year=start_local.year + 1)
        else:
            q_month = ((base.month - 1) // 3) * 3 + 1
            start_local = base.replace(month=q_month, day=1, hour=0, minute=0, second=0, microsecond=0)
            if q_month == 10:
                end_local = start_local.replace(year=start_local.year + 1, month=1)
            else:
                end_local = start_local.replace(month=q_month + 3)
    elif p in {"all", "all_time", "life"}:
        return None, None, "all_time"
    else:
        end_local = start_local + timedelta(days=1)
    return _iso(start_local), _iso(end_local), f"{p}:{start_local.date()}"


def _conversation_ids_for_period(con, *, person_id: str, period_start: str | None = None, period_end: str | None = None, limit: int = 1000) -> list[str]:
    where = ["1=1"]
    params: list[Any] = []
    if period_start:
        where.append("COALESCE(ended_at, started_at, created_at) >= ?")
        params.append(period_start)
    if period_end:
        where.append("COALESCE(started_at, created_at) < ?")
        params.append(period_end)
    # Do not over-filter by participants: BrainLive exported conversations often
    # carry William in turns/person metadata while participants_json may be sparse.
    params.append(limit)
    rows = _rows(con, f"SELECT conversation_id FROM conversations WHERE {' AND '.join(where)} ORDER BY COALESCE(started_at, created_at) LIMIT ?", tuple(params))
    return [str(r.get("conversation_id")) for r in rows if r.get("conversation_id")]


def _turns_for_episode(con, episode: dict[str, Any]) -> list[dict[str, Any]]:
    cid = episode.get("source_conversation_id")
    if not cid:
        return []
    turns = _rows(con, "SELECT turn_id, idx, person_id, speaker_label, start_s, end_s, text, metadata_json FROM turns WHERE conversation_id=? ORDER BY idx", (cid,))
    if not turns:
        return []
    start_id = episode.get("start_turn_id")
    end_id = episode.get("end_turn_id")
    if not start_id and not end_id:
        return turns[:80]
    idx_by_id = {t.get("turn_id"): i for i, t in enumerate(turns)}
    si = idx_by_id.get(start_id, 0)
    ei = idx_by_id.get(end_id, len(turns) - 1)
    if ei < si:
        si, ei = ei, si
    return turns[si:ei + 1]


def _case_type_from(situation: dict[str, Any] | None, episode: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    st = str((situation or {}).get("situation_type") or episode.get("episode_type") or "").lower()
    dom = str((situation or {}).get("life_domain") or "").lower()
    text = _txt(st, dom, episode.get("topic"), episode.get("situation_summary"), " ".join(str(t.get("text") or "")[:200] for t in turns[:5])).lower()
    pairs = [
        ("relationship", ["relationship", "interpersonal", "social", "person", "people", "conflict", "conversation"]),
        ("work_project", ["work", "project", "technical", "code", "client", "audit", "validation", "brain", "patch"]),
        ("choice", ["choice", "decision", "option", "choose"]),
        ("routine_habit", ["routine", "habit", "pause", "cigarette", "meal", "sleep", "walk"]),
        ("emotion_energy", ["emotion", "fatigue", "energy", "stress", "frustration", "calm"]),
        ("place_context", ["place", "location", "home", "office", "outside"]),
    ]
    for ctype, keys in pairs:
        if any(k in text for k in keys):
            return ctype
    return "life_event"


def _case_key(case_type: str, people: list[str], tags: list[str], emotion_after: str | None, outcome: str | None) -> str:
    parts = [case_type]
    if people:
        parts.append("people:" + "+".join(sorted(people)[:3]))
    useful_tags = [t for t in tags if t and t not in {"unknown", "other"}][:4]
    if useful_tags:
        parts.append("tags:" + "+".join(sorted(useful_tags)))
    if emotion_after:
        parts.append("after:" + str(emotion_after).lower()[:30])
    if outcome:
        parts.append("outcome:" + str(outcome).lower()[:40])
    return "|".join(parts)[:240]


def _episode_duration_s(episode: dict[str, Any], turns: list[dict[str, Any]]) -> float | None:
    for a, b in [(episode.get("start_time"), episode.get("end_time"))]:
        da, db = _iso_parse(a), _iso_parse(b)
        if da and db:
            return max(0.0, (db - da).total_seconds())
    starts = [_safe_float(t.get("start_s"), math.nan) for t in turns if t.get("start_s") is not None]
    ends = [_safe_float(t.get("end_s"), math.nan) for t in turns if t.get("end_s") is not None]
    starts = [x for x in starts if not math.isnan(x)]
    ends = [x for x in ends if not math.isnan(x)]
    if starts and ends:
        return max(0.0, max(ends) - min(starts))
    return None


def _collect_case_material(con, episode: dict[str, Any], person_id: str) -> dict[str, Any]:
    eid = episode.get("episode_id")
    situation = _one(con, "SELECT * FROM situation_episodes WHERE episode_id=? ORDER BY created_at DESC LIMIT 1", (eid,)) or {}
    interaction = _one(con, "SELECT * FROM interaction_episodes WHERE episode_id=? ORDER BY created_at DESC LIMIT 1", (eid,)) or {}
    states = _rows(con, "SELECT * FROM internal_state_snapshots WHERE episode_id=? ORDER BY created_at", (eid,))
    thoughts = _rows(con, "SELECT * FROM thought_hypotheses WHERE episode_id=? ORDER BY created_at LIMIT 8", (eid,))
    intentions = _rows(con, "SELECT * FROM action_intentions WHERE episode_id=? ORDER BY created_at LIMIT 8", (eid,))
    outcomes = _rows(con, "SELECT * FROM action_outcomes WHERE episode_id=? ORDER BY created_at LIMIT 8", (eid,))
    choices = _rows(con, "SELECT * FROM choice_episodes WHERE episode_id=? ORDER BY created_at LIMIT 8", (eid,))
    emotions = _rows(con, "SELECT * FROM emotion_evidence WHERE episode_id=? ORDER BY created_at LIMIT 12", (eid,))
    speech = _rows(con, "SELECT * FROM speech_acts WHERE episode_id=? ORDER BY created_at LIMIT 12", (eid,))
    turns = _turns_for_episode(con, episode)
    people: set[str] = set()
    for raw in [episode.get("participants_json"), situation.get("participants_json"), situation.get("secondary_people_json")]:
        for p in _as_list(raw):
            if isinstance(p, dict):
                pid = p.get("person_id") or p.get("id") or p.get("name")
            else:
                pid = p
            if pid and str(pid) != person_id:
                people.add(str(pid))
    for t in turns:
        pid = t.get("person_id")
        if pid and str(pid) != person_id:
            people.add(str(pid))
    for key in ["other_person_id", "target_person_id"]:
        val = interaction.get(key) or episode.get(key)
        if val and str(val) != person_id:
            people.add(str(val))
    state_before = _as_dict(episode.get("user_state_before_json")) or (states[0] if states else {})
    state_after = _as_dict(episode.get("user_state_after_json")) or (states[-1] if states else {})
    emotion_before = state_before.get("emotion") or state_before.get("dominant_emotion") or (emotions[0].get("emotion_label") if emotions else None)
    emotion_after = state_after.get("emotion") or state_after.get("dominant_emotion") or (emotions[-1].get("emotion_label") if emotions else None)
    action = episode.get("speech_or_action_summary") or " / ".join(str(x.get("intention_text") or x.get("action_text") or "") for x in intentions[:3] if x)
    choice = " / ".join(str(x.get("choice_context") or x.get("chosen_option") or "") for x in choices[:3] if x)
    outcome = episode.get("outcome_summary") or " / ".join(str(x.get("outcome_summary") or x.get("observed_outcome") or x.get("outcome") or x.get("result") or x.get("lesson") or "") for x in outcomes[:3] if x)
    tags: list[str] = []
    for x in [situation.get("situation_type"), situation.get("life_domain"), situation.get("stakes"), episode.get("episode_type"), episode.get("topic"), interaction.get("relationship_type")]:
        if x:
            tags.extend(list(_norm_tokenize(str(x)))[:4])
    # Use a few high-signal tokens from summaries, not raw full text.
    tags.extend(list(_norm_tokenize(_txt(episode.get("trigger_summary"), episode.get("situation_summary"), action, outcome)))[:10])
    seen = set(); tags = [t for t in tags if not (t in seen or seen.add(t))][:24]
    evidence = []
    for t in turns[:12]:
        md = _as_dict(t.get("metadata_json"))
        evidence.append({"source_table": "turns", "source_id": t.get("turn_id"), "text": str(t.get("text") or "")[:500], "kind": md.get("kind"), "evidence_role": md.get("evidence_role")})
    for obj_table, rows in [("internal_state_snapshots", states), ("action_intentions", intentions), ("action_outcomes", outcomes), ("choice_episodes", choices), ("emotion_evidence", emotions), ("speech_acts", speech)]:
        for r in rows[:5]:
            rid = r.get("state_id") or r.get("intention_id") or r.get("outcome_id") or r.get("choice_id") or r.get("emotion_evidence_id") or r.get("speech_act_id")
            evidence.append({"source_table": obj_table, "source_id": rid, "summary": _txt(r.get("state_summary"), r.get("intention_text"), r.get("outcome_summary") or r.get("result") or r.get("lesson"), r.get("choice_context"), r.get("signal_text"), r.get("evidence_text"))[:500]})
    return {
        "situation": situation,
        "interaction": interaction,
        "states": states,
        "thoughts": thoughts,
        "intentions": intentions,
        "outcomes": outcomes,
        "choices": choices,
        "emotions": emotions,
        "speech": speech,
        "turns": turns,
        "people": sorted(people),
        "state_before": state_before,
        "state_after": state_after,
        "emotion_before": emotion_before,
        "emotion_after": emotion_after,
        "action": action,
        "choice": choice,
        "outcome": outcome,
        "tags": tags,
        "evidence": evidence,
    }


def _quality_score(material: dict[str, Any]) -> float:
    score = 0.25
    if material.get("turns"):
        score += 0.15
    if material.get("states"):
        score += 0.12
    if material.get("intentions") or material.get("choices"):
        score += 0.13
    if material.get("outcomes") or material.get("outcome"):
        score += 0.18
    if material.get("people"):
        score += 0.08
    if material.get("tags"):
        score += 0.06
    if material.get("emotions") or material.get("emotion_after"):
        score += 0.08
    return max(0.05, min(0.98, score))


def build_observed_cases_for_conversation(conversation_id: str, *, person_id: str = "me", force: bool = False) -> dict[str, Any]:
    """Materialize V13/V14 analysed episodes as comparable empirical life cases."""
    ensure_longitudinal_case_schema()
    now = now_iso()
    built: list[str] = []
    with connect() as con:
        conv = _one(con, "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,))
        if not conv:
            return {"status": "missing_conversation", "conversation_id": conversation_id, "cases_built": 0}
        episodes = _rows(con, "SELECT * FROM episodes WHERE source_conversation_id=? AND COALESCE(lifecycle_status,'active') NOT IN ('deleted','obsolete','contradicted') ORDER BY COALESCE(start_time, created_at), episode_id", (conversation_id,))
        for ep in episodes:
            eid = str(ep.get("episode_id") or "")
            if not eid:
                continue
            if not force:
                existing = _one(con, "SELECT observed_case_id FROM brain2_observed_cases_v17 WHERE person_id=? AND episode_id=?", (person_id, eid))
                if existing:
                    built.append(str(existing.get("observed_case_id")))
                    continue
            m = _collect_case_material(con, ep, person_id)
            ctype = _case_type_from(m.get("situation"), ep, m.get("turns") or [])
            tags = list(m.get("tags") or [])
            key = _case_key(ctype, m.get("people") or [], tags, m.get("emotion_after"), m.get("outcome"))
            observed_at = ep.get("start_time") or conv.get("started_at") or ep.get("created_at") or now
            title = str(ep.get("topic") or ep.get("situation_summary") or f"Observed {ctype}")[:240]
            context = _txt(ep.get("situation_summary"), (m.get("situation") or {}).get("social_context"), (m.get("situation") or {}).get("stakes")) or title
            vector = {
                "case_type": ctype,
                "tags": tags,
                "people": m.get("people") or [],
                "emotion_before": m.get("emotion_before"),
                "emotion_after": m.get("emotion_after"),
                "activity_type": ctype,
                "place": ep.get("location_text") or (m.get("situation") or {}).get("place_explicit") or (m.get("situation") or {}).get("place_inferred"),
                "trigger_tokens": sorted(_norm_tokenize(str(ep.get("trigger_summary") or "")))[:20],
                "context_tokens": sorted(_norm_tokenize(context))[:40],
                "action_tokens": sorted(_norm_tokenize(str(m.get("action") or m.get("choice") or "")))[:40],
                "outcome_tokens": sorted(_norm_tokenize(str(m.get("outcome") or "")))[:40],
            }
            embedding_text = _txt(title, context, ep.get("trigger_summary"), m.get("action"), m.get("choice"), m.get("outcome"), " ".join(tags), " ".join(m.get("people") or []))
            ocid = stable_id("observedcase17", person_id, eid)
            quality = _quality_score(m)
            row = {
                "observed_case_id": ocid,
                "person_id": person_id,
                "conversation_id": conversation_id,
                "episode_id": eid,
                "case_type": ctype,
                "case_key": key,
                "title": title,
                "context_summary": context[:2000],
                "trigger_summary": ep.get("trigger_summary"),
                "activity_type": ctype,
                "place_text": ep.get("location_text") or (m.get("situation") or {}).get("place_explicit") or (m.get("situation") or {}).get("place_inferred"),
                "people_json": json_dumps(m.get("people") or []),
                "relation_context_json": json_dumps({"interaction": m.get("interaction") or {}, "relationship_id": (m.get("situation") or {}).get("related_relationship_id")}),
                "state_before_json": json_dumps(m.get("state_before") or {}),
                "state_after_json": json_dumps(m.get("state_after") or {}),
                "emotion_before": m.get("emotion_before"),
                "emotion_after": m.get("emotion_after"),
                "action_summary": m.get("action"),
                "choice_summary": m.get("choice"),
                "outcome_summary": m.get("outcome"),
                "duration_s": _episode_duration_s(ep, m.get("turns") or []),
                "evidence_json": json_dumps(m.get("evidence") or []),
                "counter_evidence_json": json_dumps([]),
                "tags_json": json_dumps(tags),
                "comparable_vector_json": json_dumps(vector),
                "embedding_text": embedding_text[:4000],
                "quality_score": quality,
                "confidence": max(_safe_float(ep.get("confidence"), 0.55), quality * 0.75),
                "observed_at": observed_at,
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
            upsert(con, "brain2_observed_cases_v17", row, "observed_case_id")
            # Also expose the case to the existing prediction engine as a true
            # empirical prediction_case.  This is the bridge from deep memory to
            # future similar-case retrieval.
            pcid = stable_id("predcase_observed17", person_id, eid)
            upsert(con, "prediction_cases", {
                "case_id": pcid,
                "case_type": "observed_life_case_v17",
                "episode_id": eid,
                "person_id": person_id,
                "context_summary": context[:2000],
                "situation_vector_json": json_dumps({"observed_case_id": ocid, "case_type": ctype, "tags": tags, "people": m.get("people") or [], "place": row["place_text"]}),
                "state_vector_json": json_dumps({"before": m.get("state_before") or {}, "after": m.get("state_after") or {}, "emotion_before": m.get("emotion_before"), "emotion_after": m.get("emotion_after")}),
                "action_taken": m.get("action") or m.get("choice"),
                "speech_next": None,
                "emotion_next": m.get("emotion_after"),
                "thought_next_hypothesis": _txt(*(x.get("thought_text") or x.get("hypothesis") or x.get("content") or "" for x in (m.get("thoughts") or [])[:3]))[:1000],
                "outcome": m.get("outcome"),
                "usable_for_prediction": 1 if quality >= 0.45 else 0,
                "quality_score": quality,
                "evidence_json": json_dumps({"observed_case_id": ocid, "evidence": m.get("evidence") or []}),
                "created_at": now,
                "updated_at": now,
            }, "case_id")
            built.append(ocid)
        con.commit()
    return {"status": "ok", "conversation_id": conversation_id, "cases_built": len(set(built)), "observed_case_ids": sorted(set(built))}


def build_observed_cases_for_period(*, person_id: str = "me", period_start: str | None = None, period_end: str | None = None, conversation_ids: list[str] | None = None, force: bool = False) -> dict[str, Any]:
    ensure_longitudinal_case_schema()
    with connect() as con:
        cids = conversation_ids or _conversation_ids_for_period(con, person_id=person_id, period_start=period_start, period_end=period_end)
    out = []
    total = 0
    for cid in cids:
        r = build_observed_cases_for_conversation(cid, person_id=person_id, force=force)
        out.append(r)
        total += int(r.get("cases_built") or 0)
    return {"status": "ok", "person_id": person_id, "conversation_ids": cids, "conversations_processed": len(cids), "cases_built": total, "results": out}


def _load_case(row: dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    r["tags"] = [str(x) for x in _as_list(r.get("tags_json"))]
    r["people"] = [str(x) for x in _as_list(r.get("people_json"))]
    r["vector"] = _as_dict(r.get("comparable_vector_json"))
    return r


def _case_similarity(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, dict[str, float], dict[str, Any], dict[str, Any]]:
    av, bv = a.get("vector") or {}, b.get("vector") or {}
    semantic = _jaccard(_norm_tokenize(_txt(a.get("embedding_text"), a.get("context_summary"))), _norm_tokenize(_txt(b.get("embedding_text"), b.get("context_summary"))))
    situation = 0.0
    if a.get("case_type") == b.get("case_type"):
        situation += 0.35
    situation += 0.35 * _jaccard(a.get("tags") or [], b.get("tags") or [])
    if a.get("place_text") and a.get("place_text") == b.get("place_text"):
        situation += 0.15
    if a.get("activity_type") and a.get("activity_type") == b.get("activity_type"):
        situation += 0.15
    state = 0.0
    for k in ["emotion_before", "emotion_after"]:
        if a.get(k) and a.get(k) == b.get(k):
            state += 0.25
    state += 0.25 * _jaccard(_as_list((av.get("outcome_tokens") or [])), _as_list((bv.get("outcome_tokens") or [])))
    rel = 0.55 * _jaccard(a.get("people") or [], b.get("people") or [])
    outcome = 0.7 * _jaccard(_as_list(av.get("outcome_tokens") or []), _as_list(bv.get("outcome_tokens") or []))
    language = 0.55 * _jaccard(_as_list(av.get("action_tokens") or []), _as_list(bv.get("action_tokens") or []))
    dims = {
        "semantic": min(1.0, semantic),
        "situation": min(1.0, situation),
        "state": min(1.0, state),
        "relationship": min(1.0, rel),
        "outcome": min(1.0, outcome),
        "language": min(1.0, language),
    }
    final = (
        0.22 * dims["semantic"] + 0.24 * dims["situation"] + 0.16 * dims["state"] +
        0.14 * dims["relationship"] + 0.14 * dims["outcome"] + 0.10 * dims["language"]
    )
    shared = {
        "tags": sorted(set(a.get("tags") or []) & set(b.get("tags") or []))[:20],
        "people": sorted(set(a.get("people") or []) & set(b.get("people") or []))[:10],
        "case_type_match": a.get("case_type") == b.get("case_type"),
        "same_place": bool(a.get("place_text") and a.get("place_text") == b.get("place_text")),
        "same_emotion_after": bool(a.get("emotion_after") and a.get("emotion_after") == b.get("emotion_after")),
    }
    diff = {"case_a": a.get("observed_case_id"), "case_b": b.get("observed_case_id"), "outcome_a": a.get("outcome_summary"), "outcome_b": b.get("outcome_summary")}
    return min(1.0, final), dims, shared, diff


def compute_global_case_similarities(*, person_id: str = "me", anchor_case_ids: list[str] | None = None, period_start: str | None = None, period_end: str | None = None, top_k: int = 12, min_score: float = 0.34, max_history: int = 5000) -> dict[str, Any]:
    ensure_longitudinal_case_schema()
    now = now_iso()
    with connect() as con:
        clauses = ["person_id=?", "status='active'"]
        params: list[Any] = [person_id]
        if anchor_case_ids:
            placeholders = ",".join("?" for _ in anchor_case_ids)
            clauses.append(f"observed_case_id IN ({placeholders})")
            params.extend(anchor_case_ids)
        else:
            if period_start:
                clauses.append("COALESCE(observed_at, created_at) >= ?"); params.append(period_start)
            if period_end:
                clauses.append("COALESCE(observed_at, created_at) < ?"); params.append(period_end)
        anchors = [_load_case(r) for r in _rows(con, f"SELECT * FROM brain2_observed_cases_v17 WHERE {' AND '.join(clauses)} ORDER BY COALESCE(observed_at, created_at)", tuple(params))]
        history = [_load_case(r) for r in _rows(con, "SELECT * FROM brain2_observed_cases_v17 WHERE person_id=? AND status='active' ORDER BY COALESCE(observed_at, created_at) DESC LIMIT ?", (person_id, max_history))]
        h_by_id = {h["observed_case_id"]: h for h in history}
        created = 0
        for a in anchors:
            scored = []
            for b in history:
                if a.get("observed_case_id") == b.get("observed_case_id"):
                    continue
                score, dims, shared, diff = _case_similarity(a, b)
                if score >= min_score:
                    scored.append((score, dims, shared, diff, b))
            scored.sort(key=lambda x: x[0], reverse=True)
            for score, dims, shared, diff, b in scored[:top_k]:
                eid = stable_id("caseedge17", person_id, a.get("observed_case_id"), b.get("observed_case_id"))
                upsert(con, "brain2_case_similarity_edges_v17", {
                    "edge_id": eid,
                    "person_id": person_id,
                    "anchor_case_id": a.get("observed_case_id"),
                    "similar_case_id": b.get("observed_case_id"),
                    "final_score": score,
                    "semantic_similarity": dims["semantic"],
                    "situation_similarity": dims["situation"],
                    "state_similarity": dims["state"],
                    "relationship_similarity": dims["relationship"],
                    "outcome_similarity": dims["outcome"],
                    "language_similarity": dims["language"],
                    "shared_features_json": json_dumps(shared),
                    "differences_json": json_dumps(diff),
                    "created_at": now,
                    "updated_at": now,
                }, "edge_id")
                # Also bridge into similar_case_scores for existing consumers.
                scid = stable_id("simcase17", person_id, a.get("observed_case_id"), b.get("observed_case_id"))
                pred_case = stable_id("predcase_observed17", person_id, b.get("episode_id")) if b.get("episode_id") else None
                if pred_case:
                    try:
                        upsert(con, "similar_case_scores", {
                            "similar_case_id": scid,
                            "prediction_id": None,
                            "case_id": pred_case,
                            "person_id": person_id,
                            "prediction_target": "global_life_similarity",
                            "semantic_similarity": dims["semantic"],
                            "situation_similarity": dims["situation"],
                            "state_similarity": dims["state"],
                            "relationship_similarity": dims["relationship"],
                            "outcome_similarity": dims["outcome"],
                            "language_similarity": dims["language"],
                            "final_score": score,
                            "explanation": f"Observed case {a.get('observed_case_id')} resembles {b.get('observed_case_id')} with shared={shared}",
                            "metadata_json": json_dumps({"anchor_observed_case_id": a.get("observed_case_id"), "similar_observed_case_id": b.get("observed_case_id"), "v17_global": True}),
                            "created_at": now,
                        }, "similar_case_id")
                    except Exception:
                        pass
                created += 1
        con.commit()
    return {"status": "ok", "person_id": person_id, "anchors": len(anchors), "history": len(history), "edges_upserted": created}


def _pattern_key_for_case(c: dict[str, Any]) -> str:
    people = "+".join(sorted(c.get("people") or [])[:2])
    tags = "+".join(sorted(c.get("tags") or [])[:5])
    emo = str(c.get("emotion_after") or "").lower()[:28]
    return f"{c.get('case_type')}|{people}|{tags}|{emo}"[:240]


def _counterexamples_for_group(group: list[dict[str, Any]], all_cases: list[dict[str, Any]], max_items: int = 8) -> list[str]:
    if not group:
        return []
    tags = set().union(*(set(c.get("tags") or []) for c in group))
    ctype = group[0].get("case_type")
    out_tokens = set().union(*(_norm_tokenize(str(c.get("outcome_summary") or "")) for c in group))
    counters = []
    g_ids = {c.get("observed_case_id") for c in group}
    for c in all_cases:
        if c.get("observed_case_id") in g_ids:
            continue
        tag_sim = _jaccard(tags, c.get("tags") or [])
        if c.get("case_type") == ctype and tag_sim > 0.18:
            out_sim = _jaccard(out_tokens, _norm_tokenize(str(c.get("outcome_summary") or "")))
            if out_sim < 0.12 or (group[0].get("emotion_after") and c.get("emotion_after") and c.get("emotion_after") != group[0].get("emotion_after")):
                counters.append(str(c.get("observed_case_id")))
                if len(counters) >= max_items:
                    break
    return counters


def mine_global_life_patterns(*, person_id: str = "me", period_start: str | None = None, period_end: str | None = None, min_recurrence: int = 3, max_cases: int = 5000) -> dict[str, Any]:
    ensure_longitudinal_case_schema()
    now = now_iso()
    with connect() as con:
        all_cases = [_load_case(r) for r in _rows(con, "SELECT * FROM brain2_observed_cases_v17 WHERE person_id=? AND status='active' ORDER BY COALESCE(observed_at, created_at) DESC LIMIT ?", (person_id, max_cases))]
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in all_cases:
            groups[_pattern_key_for_case(c)].append(c)
        pattern_ids: list[str] = []
        for key, group in groups.items():
            if len(group) < min_recurrence:
                continue
            group.sort(key=lambda x: str(x.get("observed_at") or x.get("created_at") or ""))
            contexts = sorted({str(c.get("case_type") or "") for c in group} | set(t for c in group for t in (c.get("tags") or [])[:5]))[:30]
            people = sorted(set(p for c in group for p in (c.get("people") or [])))[:20]
            outcomes = [str(c.get("outcome_summary") or "") for c in group if c.get("outcome_summary")]
            triggers = [str(c.get("trigger_summary") or "") for c in group if c.get("trigger_summary")]
            actions = [str(c.get("action_summary") or c.get("choice_summary") or "") for c in group if c.get("action_summary") or c.get("choice_summary")]
            emos = [str(c.get("emotion_after") or "") for c in group if c.get("emotion_after")]
            cex = _counterexamples_for_group(group, all_cases)
            recurrence = len(group)
            context_count = len(contexts)
            confidence = min(0.95, 0.35 + recurrence * 0.08 + min(context_count, 6) * 0.03 - len(cex) * 0.025)
            status = "confirmed" if recurrence >= 5 and confidence >= 0.65 else "candidate"
            stratum = "general" if recurrence >= 8 and len({(c.get("observed_at") or "")[:10] for c in group}) >= 7 else "recent"
            case_type = group[0].get("case_type") or "life_event"
            title = f"{case_type.replace('_', ' ')} pattern across {recurrence} observed scenes"
            common_tags = [x for x, _ in Counter(t for c in group for t in c.get("tags") or []).most_common(8)]
            usual_outcome = Counter(outcomes).most_common(1)[0][0] if outcomes else None
            usual_trigger = Counter(triggers).most_common(1)[0][0] if triggers else None
            usual_action = Counter(actions).most_common(1)[0][0] if actions else None
            usual_state = Counter(emos).most_common(1)[0][0] if emos else None
            description = _txt(
                f"Repeated {case_type} structure observed {recurrence} times.",
                f"Common tags: {', '.join(common_tags)}." if common_tags else "",
                f"Usual trigger: {usual_trigger}." if usual_trigger else "",
                f"Usual action: {usual_action}." if usual_action else "",
                f"Usual outcome: {usual_outcome}." if usual_outcome else "",
            )
            pid = stable_id("globalpattern17", person_id, key)
            row = {
                "pattern_id": pid,
                "person_id": person_id,
                "pattern_type": str(case_type),
                "pattern_key": key,
                "title": title[:240],
                "description": description[:3000] or title,
                "recurrence_count": recurrence,
                "context_count": context_count,
                "people_count": len(people),
                "counterexample_count": len(cex),
                "first_seen": group[0].get("observed_at") or group[0].get("created_at"),
                "last_seen": group[-1].get("observed_at") or group[-1].get("created_at"),
                "evidence_case_ids_json": json_dumps([c.get("observed_case_id") for c in group]),
                "counterexample_case_ids_json": json_dumps(cex),
                "contexts_json": json_dumps(contexts),
                "people_json": json_dumps(people),
                "usual_trigger": usual_trigger,
                "usual_state_before": None,
                "usual_action": usual_action,
                "usual_outcome": usual_outcome,
                "hidden_loop_hypothesis": description[:2000],
                "confidence": max(0.1, min(0.98, confidence)),
                "status": status,
                "stratum": stratum,
                "metadata_json": json_dumps({"common_tags": common_tags, "version": VERSION, "period_start": period_start, "period_end": period_end}),
                "created_at": now,
                "updated_at": now,
            }
            upsert(con, "brain2_global_life_patterns_v17", row, "pattern_id")
            # Mirror into existing pattern tables so V14/V15 readers inherit it.
            cand_id = stable_id("candpat_v17", person_id, key)
            upsert(con, "candidate_patterns", {
                "candidate_pattern_id": cand_id,
                "person_id": person_id,
                "pattern_type": case_type,
                "pattern_key": key,
                "title": title[:240],
                "description": description[:3000] or title,
                "evidence_count": recurrence,
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "activation_contexts_json": json_dumps(contexts),
                "counterexamples_json": json_dumps(cex),
                "status": status,
                "confidence": row["confidence"],
                "metadata_json": json_dumps({"global_pattern_id": pid, "evidence_case_ids": [c.get("observed_case_id") for c in group], "v17_global": True}),
                "created_at": now,
                "updated_at": now,
            }, "candidate_pattern_id")
            if status == "confirmed":
                conf_id = stable_id("confpat_v17", person_id, key)
                upsert(con, "confirmed_patterns", {
                    "confirmed_pattern_id": conf_id,
                    "candidate_pattern_id": cand_id,
                    "person_id": person_id,
                    "pattern_type": case_type,
                    "pattern_key": key,
                    "title": title[:240],
                    "description": description[:3000] or title,
                    "evidence_count": recurrence,
                    "counterexample_count": len(cex),
                    "activation_conditions_json": json_dumps(contexts),
                    "escape_conditions_json": json_dumps([]),
                    "usual_outcome": usual_outcome,
                    "confidence": row["confidence"],
                    "validity_status": "active",
                    "metadata_json": json_dumps({"global_pattern_id": pid, "evidence_case_ids": [c.get("observed_case_id") for c in group], "v17_global": True}),
                    "created_at": now,
                    "updated_at": now,
                }, "confirmed_pattern_id")
            pattern_ids.append(pid)
        con.commit()
    return {"status": "ok", "person_id": person_id, "cases_considered": len(all_cases), "patterns_upserted": len(pattern_ids), "pattern_ids": pattern_ids}


def run_longitudinal_consolidation(
    *,
    person_id: str = "me",
    period: str = "day",
    run_date: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    use_llm: bool = True,
    run_periodic_mirror_layer: bool = True,
    force_cases: bool = False,
) -> dict[str, Any]:
    """One command for day/week/month longitudinal memory consolidation."""
    ensure_longitudinal_case_schema()
    start, end, label = period_bounds(period, run_date=run_date, period_start=period_start, period_end=period_end)
    now = now_iso()
    run_id = stable_id("longrun17", person_id, period, start or "", end or "", now)
    status = "ok"
    error_text = None
    result: dict[str, Any] = {"period_label": label, "period_start": start, "period_end": end}
    try:
        cases = build_observed_cases_for_period(person_id=person_id, period_start=start, period_end=end, force=force_cases)
        result["observed_cases"] = cases
        anchor_ids: list[str] = []
        with connect() as con:
            clauses = ["person_id=?", "status='active'"]
            params: list[Any] = [person_id]
            if start:
                clauses.append("COALESCE(observed_at, created_at) >= ?"); params.append(start)
            if end:
                clauses.append("COALESCE(observed_at, created_at) < ?"); params.append(end)
            anchor_ids = [str(r.get("observed_case_id")) for r in _rows(con, f"SELECT observed_case_id FROM brain2_observed_cases_v17 WHERE {' AND '.join(clauses)}", tuple(params))]
        sim = compute_global_case_similarities(person_id=person_id, anchor_case_ids=anchor_ids, period_start=start, period_end=end)
        result["similarity"] = sim
        pats = mine_global_life_patterns(person_id=person_id, period_start=start, period_end=end, min_recurrence=2 if period in {"day", "hour"} else 3)
        result["global_patterns"] = pats
        if run_periodic_mirror_layer and use_llm:
            try:
                from .pattern_mirror_v14 import run_periodic_mirror
                result["periodic_mirror_v14"] = run_periodic_mirror(person_id=person_id, period=period, period_start=start, period_end=end)
            except Exception as exc:
                result["periodic_mirror_v14"] = {"status": "error", "error": str(exc)[:1000]}
                status = "partial"
    except Exception as exc:
        status = "error"; error_text = str(exc)[:2000]
        result["error"] = error_text
    with connect() as con:
        upsert(con, "brain2_longitudinal_runs_v17", {
            "run_id": run_id,
            "person_id": person_id,
            "period": period,
            "period_start": start,
            "period_end": end,
            "status": status,
            "conversations_processed": int((result.get("observed_cases") or {}).get("conversations_processed") or 0),
            "cases_built": int((result.get("observed_cases") or {}).get("cases_built") or 0),
            "similarity_edges": int((result.get("similarity") or {}).get("edges_upserted") or 0),
            "patterns_upserted": int((result.get("global_patterns") or {}).get("patterns_upserted") or 0),
            "results_json": json_dumps(result),
            "error_text": error_text,
            "created_at": now,
            "updated_at": now_iso(),
        }, "run_id")
        con.commit()
    return {"version": VERSION, "run_id": run_id, "person_id": person_id, "period": period, "status": status, **result}


def longitudinal_memory_digest(person_id: str = "me", *, limit: int = 30) -> dict[str, Any]:
    ensure_longitudinal_case_schema()
    with connect() as con:
        patterns = _rows(con, "SELECT * FROM brain2_global_life_patterns_v17 WHERE person_id=? ORDER BY confidence DESC, recurrence_count DESC LIMIT ?", (person_id, limit))
        cases = _rows(con, "SELECT * FROM brain2_observed_cases_v17 WHERE person_id=? ORDER BY COALESCE(observed_at, created_at) DESC LIMIT ?", (person_id, limit))
        runs = _rows(con, "SELECT * FROM brain2_longitudinal_runs_v17 WHERE person_id=? ORDER BY created_at DESC LIMIT 10", (person_id,))
    return {"person_id": person_id, "patterns": patterns, "recent_cases": cases, "runs": runs}

# V18 remediation overrides: ownership scope, local episode bounds, causal
# similarity, retrievable versions, and stale projection retirement.
from .v18_longitudinal import install as _install_v18_longitudinal
_globals_v18_longitudinal = _install_v18_longitudinal(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_longitudinal)
