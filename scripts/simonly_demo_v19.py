from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mlomega_audio_elite.brainlive_v15 import start_live_session
from mlomega_audio_elite.db import connect
from mlomega_audio_elite.v18_delivery import enqueue_delivery
from packages.contracts.python.models import UIReceipt
from simulators.fake_xr_device import FakeXrDevice


def _load_delivery_adapter():
    path = ROOT / "services" / "live-pc" / "delivery_adapter.py"
    spec = importlib.util.spec_from_file_location("v19_delivery_adapter_demo", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def run_demo() -> dict[str, object]:
    db_path = Path(os.environ.get("MLOMEGA_DB", ROOT / ".mlomega_v19_simonly.db"))
    os.environ.setdefault("MLOMEGA_HOME", str(db_path.parent / ".mlomega_home"))
    os.environ["MLOMEGA_DB"] = str(db_path)
    os.environ.setdefault("MLOMEGA_ENABLE_OLLAMA", "false")

    device = FakeXrDevice(frames=1, fps=0)
    first_frame = None
    async for _frame, envelope in device.stream():
        first_frame = envelope
        break
    if first_frame is None:
        raise RuntimeError("fake device produced no frame")

    session = start_live_session(person_id="me", title="V19 SimOnly checkpoint demo")
    queued = enqueue_delivery(
        live_session_id=session["live_session_id"],
        source_key=f"v19-simonly:{first_frame.frame_id}",
        candidate={
            "candidate_id": "v19-simonly-uiintent-test",
            "message": "V19 SimOnly UIIntent test card",
            "action_type": "notify",
            "decision": "queue",
            "priority": 0.9,
            "evidence_refs": [first_frame.frame_id],
        },
    )
    if queued.get("status") not in {"queued", "deduplicated"} or not queued.get("delivery_id"):
        raise RuntimeError(f"delivery enqueue failed: {queued}")

    delivery_adapter = _load_delivery_adapter()
    adapter = delivery_adapter.DeliveryAdapter()
    intents = await adapter.dispatch_once()
    if not intents:
        raise RuntimeError("delivery adapter produced no UIIntent")
    intent = intents[0]

    receipt = UIReceipt(
        ui_intent_id=intent.ui_intent_id,
        delivery_id=intent.delivery_id,
        event="displayed",
        observed_at=datetime.now(timezone.utc).isoformat(),
        local_track_state={"simulated": True, "frame_id": first_frame.frame_id},
        source="companion_web_simulator",
    )
    adapter.record_receipt(receipt)

    with connect() as con:
        con.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in con.execute(
                """SELECT delivery_id, feedback_type, feedback_source, evidence_json
                   FROM brainlive_intervention_feedback_events_v188
                   WHERE delivery_id=? ORDER BY observed_at""",
                (intent.delivery_id,),
            ).fetchall()
        ]
    if not any(row["feedback_type"] == "displayed" for row in rows):
        raise RuntimeError("displayed UIReceipt not visible in brainlive_intervention_feedback_events_v188")

    return {
        "status": "ok",
        "db_path": str(db_path),
        "frame_id": first_frame.frame_id,
        "delivery_id": intent.delivery_id,
        "ui_intent_id": intent.ui_intent_id,
        "feedback_events": rows,
    }


def main() -> None:
    print(json.dumps(asyncio.run(run_demo()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
