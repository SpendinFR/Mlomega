"""V18 isolated replay: no production BrainLive session, no current-time rewrite."""
from __future__ import annotations
from typing import Any

from .db import connect, write_transaction, upsert
from .governance_v18 import Scope, ScopeError, build_context_manifest, conversation_in_scope, ensure_v18_schema, register_conversation_scope, ContextItem
from .integrity_v176 import iso_utc, parse_iso_utc
from .utils import json_dumps, now_iso, stable_id


def _turn_time(conv_started: str | None, turn: dict[str, Any]) -> str:
    """Return a verifiable source time or reject the replay input.

    A replay is a causal evaluation.  Silently dropping a malformed turn or
    assigning every untimed turn the conversation start creates a flattering
    but false backtest.  Direct per-turn timestamps win; otherwise a valid
    conversation anchor plus a finite non-negative ``start_s`` is required.
    """
    meta = turn.get("metadata_json")
    if isinstance(meta, str):
        try:
            from .utils import json_loads
            meta = json_loads(meta, {})
        except Exception as exc:
            raise ScopeError(f"replay rejected: invalid turn metadata for {turn.get('turn_id')}: {exc}") from exc
    if meta is not None and not isinstance(meta, dict):
        raise ScopeError(f"replay rejected: non-object turn metadata for {turn.get('turn_id')}")
    if isinstance(meta, dict):
        for key in ("occurred_at", "timestamp_start", "captured_at", "recorded_at"):
            if meta.get(key) is not None:
                try:
                    return iso_utc(parse_iso_utc(str(meta[key])))
                except Exception as exc:
                    raise ScopeError(
                        f"replay rejected: invalid {key} for turn {turn.get('turn_id')}: {meta[key]!r}"
                    ) from exc
    if not conv_started:
        raise ScopeError(f"replay rejected: turn {turn.get('turn_id')} has no source anchor")
    try:
        anchor = parse_iso_utc(conv_started)
    except Exception as exc:
        raise ScopeError(f"replay rejected: invalid conversation start for {turn.get('turn_id')}") from exc
    if turn.get("start_s") is None:
        raise ScopeError(f"replay rejected: turn {turn.get('turn_id')} has no per-turn source offset")
    try:
        offset = float(turn["start_s"])
    except (TypeError, ValueError) as exc:
        raise ScopeError(f"replay rejected: invalid start_s for turn {turn.get('turn_id')}") from exc
    import math
    if not math.isfinite(offset) or offset < 0:
        raise ScopeError(f"replay rejected: invalid start_s for turn {turn.get('turn_id')}")
    from datetime import timedelta
    return iso_utc(anchor + timedelta(seconds=offset))


def install(module: Any) -> dict[str,Any]:
    old_ensure = module.ensure_longitudinal_schema

    def replay_offline(*, person_id: str | None = None, conversation_id: str | None = None,
                       start_time: str | None = None, end_time: str | None = None,
                       step_turns: int = 8, timeout: float = 480.0) -> dict[str,Any]:
        if not person_id or not conversation_id:
            raise ScopeError("V18 replay requires explicit person_id and conversation_id")
        # Fresh databases need the legacy replay read-model table as well as
        # governance tables.  A replay must not fail only because no live loop
        # has ever run on this installation.
        old_ensure()
        ensure_v18_schema()
        needs_owner_migration = False
        with connect() as con:
            conv=con.execute("SELECT * FROM conversations WHERE conversation_id=?",(conversation_id,)).fetchone()
            if not conv: raise ScopeError("conversation introuvable")
            # Compatibility migration only when direct turn ownership proves it.
            # Register after this read connection has closed; otherwise a second
            # writer can deadlock against a long-lived reader in SQLite.
            if not conversation_in_scope(
                con, conversation_id=conversation_id, person_id=person_id,
                allow_legacy_turn_proof=False,
            ):
                owned=con.execute("SELECT COUNT(*) AS c FROM turns WHERE conversation_id=? AND person_id=?",(conversation_id,person_id)).fetchone()
                if not owned or int(owned["c"] or 0)==0:
                    raise ScopeError("replay conversation outside requested person scope")
                needs_owner_migration = True
            turns=[dict(r) for r in con.execute("SELECT * FROM turns WHERE conversation_id=? ORDER BY idx,turn_id",(conversation_id,))]
            started=conv["started_at"]
        if needs_owner_migration:
            register_conversation_scope(
                conversation_id=conversation_id, person_id=person_id,
                evidence_kind="turn_owner", evidence={"migration":"replay"},
            )
        temporal=[]
        for t in turns:
            event_time = _turn_time(started, t)
            temporal.append((parse_iso_utc(event_time), {**t, "event_time": event_time}))
        if not temporal:
            raise ScopeError("replay rejected: conversation contains no turns")
        temporal.sort(key=lambda x:x[0])
        chosen_start=iso_utc(parse_iso_utc(start_time)) if start_time else iso_utc(temporal[0][0])
        chosen_end=iso_utc(parse_iso_utc(end_time)) if end_time else iso_utc(temporal[-1][0])
        if parse_iso_utc(chosen_end)<parse_iso_utc(chosen_start): raise ScopeError("replay end before start")
        selected=[t for dt,t in temporal if parse_iso_utc(chosen_start)<=dt<=parse_iso_utc(chosen_end)]
        if not selected:
            raise ScopeError("replay window contains no turn")
        as_of=chosen_end
        scope=Scope(person_id=person_id,as_of=as_of,mode="replay")
        replay_id=stable_id("replay18",person_id,conversation_id,chosen_start,chosen_end,now_iso())
        namespace=f"replay:{replay_id}"
        items=[]
        for t in selected:
            items.append(ContextItem(source_table="turns",source_id=str(t["turn_id"]),person_id=person_id,occurred_at=t["event_time"],text=str(t.get("text") or ""),importance=1.0,metadata={"idx":t.get("idx"),"speaker_label":t.get("speaker_label")},retrievable=True))
        manifest=build_context_manifest(scope=scope,purpose="isolated_historical_replay",items=items,max_chars=60000,run_id=replay_id)
        chunks=[]
        for i in range(0,len(selected),max(1,int(step_turns))):
            chunk=selected[i:i+max(1,int(step_turns))]
            chunks.append({"step":len(chunks)+1,"as_of":chunk[-1]["event_time"],"turn_ids":[x["turn_id"] for x in chunk],"source_times":[x["event_time"] for x in chunk]})
        result={"version":"18.0.0","mode":"isolated_replay","namespace":namespace,"conversation_id":conversation_id,"person_id":person_id,"window":{"start":chosen_start,"end":chosen_end,"as_of":as_of},"turn_count":len(selected),"steps":chunks,"context_manifest_id":manifest["context_id"],"incomplete_context":manifest["incomplete"],"llm_execution":"not_written_to_production"}
        with connect() as con,write_transaction(con):
            con.execute("""INSERT INTO v18_replay_runs(replay_id,person_id,conversation_id,start_time,end_time,as_of,status,isolated_namespace,result_json,created_at,finished_at,error_text)
                         VALUES(?,?,?,?,?,?, 'completed',?,?,?, ?,NULL)""",(replay_id,person_id,conversation_id,chosen_start,chosen_end,as_of,namespace,json_dumps(result),now_iso(),now_iso()))
            # Compatibility read-model only: no brainlive session or turn row.
            upsert(con,"brainlive_replay_runs",{"replay_id":replay_id,"person_id":person_id,"source":"v18_isolated_replay","start_time":chosen_start,"end_time":chosen_end,"status":"completed","counts_json":json_dumps({"turns":len(selected),"steps":len(chunks)}),"result_json":json_dumps(result),"created_at":now_iso(),"error_text":None},"replay_id")
        return {"replay_id":replay_id,"status":"completed",**result}
    return {"replay_offline":replay_offline}
