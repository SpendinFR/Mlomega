from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

class MemoryBridge:
    def __init__(self, store: Any, post_json: Callable[[str, dict[str, Any]], dict[str, Any]]):
        self.store=store; self.post_json=post_json

    def ingest_trigger(self, trigger: dict[str, Any]) -> dict[str, Any]:
        trig = str(trigger.get('trigger') or trigger.get('event_type') or '')
        if trig not in {'explicit_keep','replay_request','what_is_this','reliable_change','personal_object','brainlive_displayed'}:
            return {'status':'ignored','reason':'non_selecting_trigger'}
        evidence=list(trigger.get('evidence') or [])
        if trigger.get('asset_bytes') is not None:
            evidence.append(self.store.store_bytes(trigger['asset_bytes'], kind=trigger.get('asset_kind') or 'keyframe', metadata={'trigger':trig}))
        elif trigger.get('asset_path'):
            evidence.append(self.store.copy_asset(Path(trigger['asset_path']), kind=trigger.get('asset_kind') or 'keyframe', metadata={'trigger':trig}))
        payload={**trigger, 'event_type': trigger.get('event_type') or trig, 'evidence': evidence}
        return {'status':'posted', 'response': self.post_json('/ingest/visual-event', payload), 'evidence': evidence}
