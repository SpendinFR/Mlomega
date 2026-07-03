from __future__ import annotations
from typing import Any
from .v19_visual_store import ensure_v19_visual_schema, store_scene_summary
from .utils import now_iso

def run_visual_consolidation(*, person_id: str, package_date: str, live_session_id: str | None = None, db_path=None) -> dict[str, Any]:
    ensure_v19_visual_schema(db_path)
    from .db import connect
    with connect(db_path) as con:
        q='SELECT * FROM visual_events_v19 WHERE person_id=?'; params=[person_id]
        if live_session_id: q+=' AND live_session_id=?'; params.append(live_session_id)
        rows=[dict(r) for r in con.execute(q, tuple(params)).fetchall()]
    if rows:
        sid=store_scene_summary({'memory_owner_id':person_id,'live_session_id':live_session_id or rows[-1]['live_session_id'],'summary_start':rows[0]['occurred_at'],'summary_end':rows[-1]['occurred_at'],'summary':{'event_count':len(rows),'event_types':sorted({r['event_type'] for r in rows})},'evidence_refs':[{'source_table':'visual_events_v19','source_id':r['visual_event_id']} for r in rows[:20]]}, db_path=db_path)
    else: sid=None
    return {'status':'completed','stage':'visual_consolidation','summary_id':sid,'visual_event_count':len(rows),'package_date':package_date}
