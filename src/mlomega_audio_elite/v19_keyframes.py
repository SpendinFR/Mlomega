from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping
from .db import connect, init_db, insert_only, write_transaction
from .utils import now_iso, stable_id, json_dumps
import hashlib

def register_xr_keyframe(*, person_id: str, live_session_id: str, image_path: str, captured_at: str | None = None, frame_id: str | None = None, metadata: Mapping[str, Any] | None = None, db_path=None) -> str:
    # E37 §4: the vision of a session is ALWAYS the wearer's — a keyframe without an
    # owner is a structural error, never a silent None. person_id is authoritative and
    # cannot be overridden/nulled by a metadata payload; it is always the last word.
    person_id = str(person_id or "").strip()
    if not person_id:
        raise ValueError("register_xr_keyframe requires a non-empty person_id (owner attribution, E37 §4)")
    init_db(db_path)
    from .brainlive_v15 import ensure_brainlive_schema
    import os
    old=os.environ.get("MLOMEGA_DB")
    if db_path is not None: os.environ["MLOMEGA_DB"]=str(db_path)
    ensure_brainlive_schema()
    if old is not None: os.environ["MLOMEGA_DB"]=old
    p=Path(image_path); data=p.read_bytes(); sha=hashlib.sha256(data).hexdigest(); now=now_iso(); captured_at=captured_at or now
    asset_id=stable_id('rawasset', str(p), sha); frame_id=frame_id or stable_id('xrframe', live_session_id, captured_at, sha)
    meta={**dict(metadata or {}), 'person_id':person_id}  # person_id last: never nulled by metadata
    with connect(db_path) as con, write_transaction(con):
        insert_only(con,'raw_assets',{'asset_id':asset_id,'type':'image','path':str(p),'sha256':sha,'captured_at':captured_at,'source':'xr_keyframe','metadata_json':json_dumps(meta),'created_at':now}, on_conflict='ignore')
        insert_only(con,'vision_frames',{'frame_id':frame_id,'source_asset_id':asset_id,'conversation_id':None,'live_session_id':live_session_id,'captured_at':captured_at,'image_path':str(p),'image_sha256':sha,'width':None,'height':None,'device_source':'xr','capture_mode':'xr_keyframe','metadata_json':json_dumps(meta),'created_at':now}, on_conflict='ignore')
    return frame_id
