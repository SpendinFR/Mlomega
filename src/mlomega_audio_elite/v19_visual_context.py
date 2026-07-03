from __future__ import annotations
from typing import Any
from .v19_self_schema import ensure_self_schema, get_self_schema
from .db import connect, init_db, insert_only, write_transaction
from .utils import json_dumps, now_iso, stable_id
SCHEMA='''CREATE TABLE IF NOT EXISTS brainlive_world_states (world_state_id TEXT PRIMARY KEY, person_id TEXT NOT NULL, live_session_id TEXT, state_time TEXT NOT NULL, place TEXT, confidence REAL, state_json TEXT DEFAULT '{}', created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS vision_scene_observations (observation_id TEXT PRIMARY KEY, person_id TEXT, conversation_id TEXT, live_session_id TEXT, frame_id TEXT, label TEXT, confidence REAL, observation_json TEXT DEFAULT '{}', created_at TEXT NOT NULL);'''
def ensure_visual_context_schema(db_path=None):
    init_db(db_path); ensure_self_schema(db_path)
    with connect(db_path) as con, write_transaction(con): con.executescript(SCHEMA)
def publish_visual_context(*, person_id: str, live_session_id: str, world_state: dict[str, Any] | None=None, observations: list[dict[str, Any]] | None=None, db_path=None) -> dict[str, Any]:
    ensure_visual_context_schema(db_path); now=now_iso(); ids=[]
    with connect(db_path) as con, write_transaction(con):
        if world_state is not None:
            wid=stable_id('worldstate', person_id, live_session_id, now)
            insert_only(con,'brainlive_world_states',{'world_state_id':wid,'person_id':person_id,'live_session_id':live_session_id,'state_time':world_state.get('state_time') or now,'place':world_state.get('place'),'confidence':world_state.get('confidence',0.8),'state_json':json_dumps(world_state),'created_at':now}, on_conflict='ignore'); ids.append(wid)
        for obs in observations or []:
            oid=obs.get('observation_id') or stable_id('obs', person_id, live_session_id, obs.get('frame_id'), obs.get('label'), now)
            insert_only(con,'vision_scene_observations',{'observation_id':oid,'person_id':person_id,'conversation_id':obs.get('conversation_id'),'live_session_id':live_session_id,'frame_id':obs.get('frame_id'),'label':obs.get('label'),'confidence':obs.get('confidence',0.8),'observation_json':json_dumps(obs),'created_at':obs.get('created_at') or now}, on_conflict='ignore'); ids.append(oid)
    return {'status':'completed','ids':ids,'self_schema_hot':get_self_schema(person_id=person_id, db_path=db_path, limit=5),'scene_focus':(world_state or {}).get('focus')}
