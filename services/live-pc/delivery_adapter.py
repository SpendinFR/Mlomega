from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from packages.contracts.python.models import UIIntent, UIReceipt
from mlomega_audio_elite.db import connect, write_transaction
from mlomega_audio_elite.utils import json_loads
from mlomega_audio_elite.v18_8_live_policy import record_delivery_feedback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def delivery_row_to_ui_intent(row: sqlite3.Row | dict[str, Any]) -> UIIntent:
    data = dict(row)
    evidence = json_loads(data.get("evidence_json") or "{}") or {}
    refs = evidence.get("evidence_refs") or evidence.get("refs") or []
    if isinstance(refs, str):
        refs = [refs]
    return UIIntent(
        ui_intent_id=f"ui-{uuid.uuid4()}", producer="brainlive", source_frame_id=None,
        component="context_card", anchor={"type": "panel", "position": "side"},
        content={"message": data.get("message") or "", "action_type": data.get("action_type") or "notify"},
        truth_level="inferred", confidence=1.0, priority=int(float(data.get("priority") or 0) * 100),
        ttl_ms=15000, evidence_refs=list(refs), delivery_id=data.get("delivery_id"),
    )


@dataclass
class RendererHub:
    sent: list[UIIntent] = field(default_factory=list)

    async def push(self, intent: UIIntent) -> None:
        self.sent.append(intent)


class DeliveryAdapter:
    def __init__(self, renderer: RendererHub | None = None) -> None:
        self.renderer = renderer or RendererHub()

    def poll_queued(self, *, limit: int = 20) -> list[sqlite3.Row]:
        with connect() as con:
            return list(con.execute(
                """SELECT * FROM brainlive_intervention_delivery_queue
                   WHERE delivery_status='queued' ORDER BY priority DESC, created_at ASC LIMIT ?""", (limit,)
            ).fetchall())

    async def dispatch_once(self) -> list[UIIntent]:
        intents: list[UIIntent] = []
        for row in self.poll_queued():
            intent = delivery_row_to_ui_intent(row)
            await self.renderer.push(intent)
            record_delivery_feedback(delivery_id=str(row["delivery_id"]), feedback_type="delivered", feedback_source="xr_adapter", evidence={"ui_intent_id": intent.ui_intent_id})
            intents.append(intent)
        return intents

    def record_receipt(self, receipt: UIReceipt) -> dict[str, Any] | None:
        if not receipt.delivery_id:
            return None
        return record_delivery_feedback(
            delivery_id=receipt.delivery_id, feedback_type=receipt.event, feedback_source="xr_adapter",
            observed_at=receipt.observed_at, evidence=receipt.model_dump(),
        )


async def main_loop(interval_s: float = 0.5) -> None:
    adapter = DeliveryAdapter()
    while True:
        await adapter.dispatch_once()
        await asyncio.sleep(interval_s)
