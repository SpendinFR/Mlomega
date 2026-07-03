"""V18 Context Gateway for BrainLive: bounded, scoped, provenance-preserving."""
from __future__ import annotations
from datetime import timedelta
import os
import re
from typing import Any, Mapping, Sequence

from .db import connect
from .governance_v18 import ContextItem, Scope, canonical_time, build_context_manifest, projection_is_active
from .integrity_v176 import parse_iso_utc
from .utils import json_dumps, now_iso, stable_id

_INVALID={"obsolete","invalidated","deleted","retracted","superseded","quarantined","error","failed","rejected","contradicted"}

_FIELD_TABLE={
    "self_model":"self_model_dimensions", "memory_cards":"memory_cards", "recent_predictions":"predictions",
    "future_scenarios":"future_scenarios", "trajectory_warnings":"trajectory_warnings", "v14_pattern_cards":"v14_pattern_mirror_cards",
    "v14_forecasts":"v14_trajectory_forecasts", "v14_forecast_watch_queue":"v14_forecast_watch_queue",
    "v14_open_loops":"v14_5_personal_open_loops", "v14_active_questions":"v14_5_active_questions",
    "v14_next_best_actions":"v14_5_next_best_actions", "v14_interpersonal_loops":"v14_6_interpersonal_loop_cards",
    "v14_relationship_models":"v14_6_relationship_state_models", "v14_social_aftereffects":"v14_6_social_aftereffects",
    "v14_intervention_suggestions":"v14_6_intervention_suggestions", "v14_intervention_policy":"v14_7_intervention_policies",
    "v14_intervention_feedback":"v14_7_intervention_feedback", "v14_clarifications":"v14_8_clarification_items",
    "brainlive_life_hypotheses":"brainlive_life_hypotheses", "v17_global_life_patterns":"brain2_global_life_patterns_v17",
    "v17_recent_observed_cases":"brain2_observed_cases_v17", "brainlive_routine_cards":"brainlive_routine_cards",
    "brainlive_affordance_matches":"brainlive_affordance_matches",
}

def _id(row: Mapping[str,Any]) -> str | None:
    for k,v in row.items():
        if k.endswith("_id") and v:
            return str(v)
    return None

def _active(row:Mapping[str,Any]) -> bool:
    # Legacy V14 rows reach this gateway only through the lifecycle bridge.
    # Its canonical state wins over the source table's historical text status.
    lifecycle=row.get("v18_lifecycle_state")
    if lifecycle is not None:
        return str(lifecycle).lower()=="open"
    status=str(row.get("status") or row.get("lifecycle_status") or row.get("truth_status") or "active").lower()
    return status not in _INVALID

def _int_env(name: str, default: int, *, minimum: int = 1, maximum: int = 100_000) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _turn_when(turn: Mapping[str, Any], fallback: str) -> str:
    return canonical_time(turn, "timestamp_start", "created_at") or fallback


def _episode_window(turns: Sequence[Mapping[str, Any]], *, as_of: str) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], dict[str, Any]]:
    """Return one recent continuous episode plus a very small pre-window.

    This deliberately runs in memory after the historical reader returns.  It
    makes the existing V13--V17 call graph cheaper and safer without changing
    how turns are persisted.  The current live episode is a suffix separated by
    an explicit time gap; it is never an arbitrary "last N conversation" blob.
    """
    gap_seconds = _int_env("MLOMEGA_V18_LIVE_EPISODE_GAP_SECONDS", 120, minimum=10, maximum=3600)
    max_turns = _int_env("MLOMEGA_V18_LIVE_EPISODE_MAX_TURNS", 12, minimum=1, maximum=64)
    before_count = _int_env("MLOMEGA_V18_LIVE_EPISODE_BEFORE_TURNS", 2, minimum=0, maximum=12)
    dated: list[tuple[Any, Mapping[str, Any]]] = []
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        try:
            dated.append((parse_iso_utc(_turn_when(turn, as_of)), turn))
        except Exception:
            # An unparseable historic record is intentionally outside the live
            # capsule.  It remains retrievable through the historical store.
            continue
    dated.sort(key=lambda pair: pair[0])
    if not dated:
        return [], [], {"episode_start_at": None, "episode_end_at": None, "gap_seconds": gap_seconds}
    start_idx = 0
    for idx in range(1, len(dated)):
        if (dated[idx][0] - dated[idx - 1][0]).total_seconds() > gap_seconds:
            start_idx = idx
    episode_pairs = dated[start_idx:]
    if len(episode_pairs) > max_turns:
        episode_pairs = episode_pairs[-max_turns:]
    actual_start = max(0, start_idx - before_count)
    # If max-turns trimmed the episode, the pre-window should immediately
    # precede the retained episode, not an older logical episode boundary.
    retained_first_index = next((i for i, pair in enumerate(dated) if pair[1] is episode_pairs[0][1]), start_idx)
    pre = dated[max(0, retained_first_index - before_count):retained_first_index]
    episode = [dict(pair[1]) for pair in episode_pairs]
    before = [dict(pair[1]) for pair in pre]
    return before, episode, {
        "episode_start_at": episode_pairs[0][0].isoformat(),
        "episode_end_at": episode_pairs[-1][0].isoformat(),
        "gap_seconds": gap_seconds,
        "max_turns": max_turns,
        "before_turns": before_count,
        "source_turn_count": len(dated),
    }


def _episode_summary(context: Mapping[str, Any], before: Sequence[Mapping[str, Any]], episode: Sequence[Mapping[str, Any]]) -> str:
    historic = str(context.get("recent_turns_summary") or "").strip()
    # Historic summary is useful only as a compact context cue; it never grants
    # evidence authority absent from the manifest references.
    historic = historic[-_int_env("MLOMEGA_V18_LIVE_HISTORY_SUMMARY_CHARS", 1400, minimum=120, maximum=6000):]
    lines: list[str] = []
    if historic:
        lines.append("Résumé historique compact: " + historic)
    local: list[str] = []
    for turn in list(before) + list(episode):
        speaker = str(turn.get("speaker_label") or turn.get("speaker_person_id") or "?")
        text = str(turn.get("text_final") or turn.get("text_partial") or "").strip()
        if text:
            local.append(f"{speaker}: {text[:320]}")
    if local:
        lines.append("Fenêtre locale: " + " | ".join(local))
    return "\n".join(lines)[:_int_env("MLOMEGA_V18_LIVE_EPISODE_SUMMARY_CHARS", 2200, minimum=240, maximum=8000)]


def _reference_rows(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in (manifest.get("items") or []) if isinstance(item, Mapping)]


def retrieve_context_references(*, person_id: str, live_session_id: str, manifest: Mapping[str, Any], refs: Sequence[Mapping[str, Any]], max_items: int = 4, max_chars: int = 5000) -> list[dict[str, Any]]:
    """Resolve explicitly requested manifest references, never arbitrary history.

    The LLM-facing code may call this only after it names a source table/id from
    the capsule.  It provides a real on-demand retrieval bridge while enforcing
    same owner, same session and ``as_of`` visibility.  The function rejects
    free-form table names and source ids not already announced by the manifest.
    """
    allowed = {(str(x.get("source_table") or ""), str(x.get("source_id") or "")): x for x in _reference_rows(manifest)}
    scope = manifest.get("scope") or {}
    if str(scope.get("person_id") or "") != str(person_id) or str(scope.get("live_session_id") or "") != str(live_session_id):
        raise ValueError("context reference retrieval scope mismatch")
    try:
        as_of = parse_iso_utc(str(scope.get("as_of")))
    except Exception as exc:
        raise ValueError("context reference retrieval requires manifest as_of") from exc
    out: list[dict[str, Any]] = []
    remaining = max(1, int(max_chars))
    with connect() as con:
        for ref in list(refs)[:max(1, int(max_items))]:
            key = (str(ref.get("source_table") or ""), str(ref.get("source_id") or ""))
            announced = allowed.get(key)
            if not announced:
                raise ValueError(f"reference was not announced by this capsule: {key[0]}/{key[1]}")
            table, source_id = key
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                raise ValueError("unsafe source table")
            columns = [dict(row) for row in con.execute(f"PRAGMA table_info({table})").fetchall()]
            if not columns:
                continue
            id_column = next((str(col["name"]) for col in columns if int(col.get("pk") or 0) == 1), None)
            if not id_column:
                id_column = next((str(col["name"]) for col in columns if str(col["name"]).endswith("_id")), None)
            if not id_column:
                continue
            row = con.execute(f"SELECT * FROM {table} WHERE {id_column}=?", (source_id,)).fetchone()
            if not row:
                continue
            payload = dict(row)
            owner = payload.get("person_id") or payload.get("subject_person_id") or person_id
            if str(owner) != str(person_id):
                raise ValueError("cross-owner reference retrieval denied")
            when = canonical_time(payload, "occurred_at", "timestamp_start", "captured_at", "updated_at", "created_at")
            if when and parse_iso_utc(when) > as_of:
                raise ValueError("future reference retrieval denied")
            rendered = json_dumps(payload)
            text = rendered[:remaining]
            out.append({"source_table": table, "source_id": source_id, "occurred_at": when, "text": text, "truncated": len(text) < len(rendered)})
            remaining -= len(text)
            if remaining <= 0:
                break
    return out


def install(module:Any)->dict[str,Any]:
    old_build=module.build_active_context
    def build_active_context(live_session_id:str,*,active_people:list[str]|None=None,refresh_minutes:int=10,limit:int=20)->dict[str,Any]:
        raw=old_build(live_session_id,active_people=active_people,refresh_minutes=refresh_minutes,limit=limit)
        context=dict(raw.get("context") or {})
        session=dict(context.get("session") or {})
        person_id=str(session.get("person_id") or "")
        if not person_id: raise ValueError("live context has no owner")
        scope=Scope(person_id=person_id,live_session_id=live_session_id,as_of=now_iso(),mode="live")
        before_turns, episode_turns, episode_meta = _episode_window(context.get("recent_turns") or [], as_of=scope.as_of_utc)
        summary_text = _episode_summary(context, before_turns, episode_turns)
        items:list[ContextItem]=[]
        # Live items are tied to the current episode plus its explicit small
        # before-window. Unknown speaker text may describe a scene but never
        # changes memory ownership.
        for turn in [*before_turns, *episode_turns]:
            if not isinstance(turn,dict): continue
            tid=_id(turn)
            when=_turn_when(turn, scope.as_of_utc)
            if tid:
                items.append(ContextItem("brainlive_turn_buffer",tid,person_id,when,str(turn.get("text_final") or turn.get("text_partial") or ""),importance=1.0,metadata={"speaker_label":turn.get("speaker_label"),"speaker_person_id":turn.get("speaker_person_id"),"episode_role":"before" if turn in before_turns else "episode"},retrievable=True))
        max_rows_per_field = _int_env("MLOMEGA_V18_LIVE_REFERENCE_ROWS_PER_FIELD", 3, minimum=1, maximum=12)
        for field,table in _FIELD_TABLE.items():
            candidate=context.get("brain2_context",{}).get(field)
            if candidate is None: candidate=context.get(field)
            rows=candidate if isinstance(candidate,list) else ([candidate] if isinstance(candidate,dict) else [])
            selected = [row for row in rows if isinstance(row,dict) and _active(row)][:max_rows_per_field]
            for row in selected:
                owner=row.get("person_id") or row.get("subject_person_id") or person_id
                if str(owner)!=person_id: continue
                rid=_id(row)
                when=canonical_time(row,"occurred_at","observed_at","updated_at","created_at")
                if not rid or not when: continue
                with connect() as con:
                    if not projection_is_active(con,projection_kind="life_model" if table.startswith("brain2_") else "context",source_table=table,source_id=rid,person_id=person_id):
                        continue
                text=json_dumps({k:v for k,v in row.items() if k not in {"raw_json","qwen_json","metadata_json"}})
                items.append(ContextItem(table,rid,person_id,when,text,importance=float(row.get("confidence") or row.get("importance_score") or 0.5),metadata={"field":field},retrievable=True))
        for kind,data in (("visual_context",context.get("visual_context")),("world_state",context.get("world_state"))):
            if isinstance(data,dict):
                rows=[]
                for val in data.values(): rows.extend(val if isinstance(val,list) else [val])
                for row in [x for x in rows if isinstance(x,dict)][:max_rows_per_field]:
                    rid=_id(row)
                    when=canonical_time(row,"captured_at","state_time","created_at")
                    if rid and when: items.append(ContextItem("vision_scene_observations" if kind=="visual_context" else "brainlive_world_states",rid,person_id,when,json_dumps(row),importance=0.8,metadata={"session":live_session_id},retrievable=True))
        manifest=build_context_manifest(
            scope=scope,purpose="brainlive_live_episode_prediction",items=items,
            max_chars=_int_env("MLOMEGA_V18_LIVE_CONTEXT_MAX_CHARS", 12000, minimum=1000, maximum=50000),
            run_id=raw.get("active_context_id"),
            max_item_chars=_int_env("MLOMEGA_V18_LIVE_CONTEXT_MAX_ITEM_CHARS", 800, minimum=120, maximum=2000),
        )
        safe={
            "schema_version":"18.4.0", "session":session,
            "active_people":context.get("active_people") or active_people or [],
            "horizons":{"H0":"0-10s","H1":"10s-5min","H2":"5min-2h"},
            "doctrine":context.get("doctrine"), "context_manifest":manifest,
            "episode": {**episode_meta, "summary": summary_text, "before_turn_ids": [_id(x) for x in before_turns if _id(x)], "turn_ids": [_id(x) for x in episode_turns if _id(x)]},
            "context_incomplete":bool(manifest.get("incomplete")),
            "retrieval_policy":"Use only source refs in manifest. A missing detail must request an explicit manifest reference; never infer omitted history.",
        }
        return {"active_context_id":raw.get("active_context_id"),"context":safe,"manifest":manifest}
    return {"build_active_context":build_active_context}

