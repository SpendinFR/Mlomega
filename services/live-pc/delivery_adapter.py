from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from packages.contracts.python.models import UIIntent, UIReceipt
from mlomega_audio_elite.db import connect
from mlomega_audio_elite.utils import json_loads
from mlomega_audio_elite.v18_8_live_policy import record_delivery_feedback

# Module-level so FastAPI can resolve the `websocket: WebSocket` annotation:
# with `from __future__ import annotations`, get_type_hints() looks the name up
# in the MODULE globals — a WebSocket imported only inside create_app() is
# invisible there and FastAPI silently degrades it to a required query param,
# closing every connection with code 1008 (bug found by the E29 phone_only e2e).
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
except ImportError:  # pragma: no cover - only without API deps installed
    FastAPI = WebSocket = WebSocketDisconnect = None  # type: ignore[assignment]


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
        truth_level="inferred", confidence=1.0, priority=max(0.0, min(1.0, float(data.get("priority") or 0.0))),
        ttl_ms=15000, evidence_refs=list(refs), delivery_id=data.get("delivery_id"),
    )


@dataclass
class RendererHub:
    sent: list[UIIntent] = field(default_factory=list)

    async def push(self, intent: UIIntent) -> None:
        self.sent.append(intent)


class WebSocketRendererHub(RendererHub):
    """Broadcast UIIntent JSON to connected companion-web/XR renderers."""

    def __init__(self) -> None:
        super().__init__()
        self._clients: set[Any] = set()

    async def connect(self, websocket: Any) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: Any) -> None:
        self._clients.discard(websocket)

    async def push(self, intent: UIIntent) -> None:
        await super().push(intent)
        if not self._clients:
            return
        payload = intent.model_dump_json()
        stale: list[Any] = []
        for websocket in list(self._clients):
            try:
                await websocket.send_text(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(websocket)


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


def create_app(adapter: DeliveryAdapter | None = None):
    """Create the V19 delivery WebSocket app used by companion-web.

    Endpoint contract:
    * GET /health returns basic readiness and connected renderer count.
    * WS /ws pushes queued BrainLive UIIntent messages as JSON.
    * Messages received on /ws are UIReceipt JSON and are persisted via V18.8 feedback.
    """
    if FastAPI is None:  # pragma: no cover - exercised only without API deps installed
        raise RuntimeError("fastapi is required for delivery_adapter.create_app()")

    renderer = adapter.renderer if adapter else WebSocketRendererHub()
    if not isinstance(renderer, WebSocketRendererHub):
        renderer = WebSocketRendererHub()
    app_adapter = adapter or DeliveryAdapter(renderer=renderer)
    app_adapter.renderer = renderer
    app = FastAPI(title="MLOmega V19 delivery adapter")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "connected_renderers": len(renderer._clients), "sent_intents": len(renderer.sent)}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await renderer.connect(websocket)
        await app_adapter.dispatch_once()
        try:
            while True:
                data = await websocket.receive_text()
                receipt = UIReceipt.model_validate_json(data)
                app_adapter.record_receipt(receipt)
        except WebSocketDisconnect:
            renderer.disconnect(websocket)

    app.state.delivery_adapter = app_adapter
    app.state.renderer = renderer
    return app


async def main_loop(interval_s: float = 0.5, adapter: DeliveryAdapter | None = None) -> None:
    adapter = adapter or DeliveryAdapter()
    while True:
        await adapter.dispatch_once()
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=8706)
