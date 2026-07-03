"""Validate the 8 V19 contracts round-trip (python -> JSON -> python).

Used by DOCTOR_MLOMEGA_V19.ps1. Exits 0 on success, 1 on failure, printing a
one-line [OK]/[FAIL] summary per contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def _samples():
    from packages.contracts.python.models import (  # noqa: E402
        EvidenceEvent,
        FrameEnvelope,
        HotSceneContext,
        LocalTrack,
        Pose,
        ReflexEvent,
        SceneDelta,
        UIIntent,
        UIReceipt,
    )

    return {
        "FrameEnvelope": FrameEnvelope(
            session_id="s1", frame_id="f1", capture_monotonic_ns=1,
            captured_at_utc="2026-07-03T00:00:00Z", pose=Pose(position=[0, 0, 0], rotation=[0, 0, 0, 1]),
            source="sim",
        ),
        "LocalTrack": LocalTrack(
            session_id="s1", track_id="t1", source_frame_id="f1", kind="object",
            bbox_or_mask={"x": 0}, velocity_screen=[0.0, 0.0], visibility=1.0,
            confidence=0.9, observed_at_monotonic_ns=1,
        ),
        "SceneDelta": SceneDelta(session_id="s1", source_frame_id="f1"),
        "ReflexEvent": ReflexEvent(
            session_id="s1", source_frame_id="f1", skill="stable_track",
            prediction={"x": 1}, horizon_ms=100, confidence=0.8, severity="info",
            aggregate_key="stable_track:t1",
        ),
        "UIIntent": UIIntent(
            ui_intent_id="u1", producer="brainlive", component="context_card",
            anchor={"type": "panel"}, content={"message": "hi"}, truth_level="inferred",
            confidence=1.0, priority=90, ttl_ms=15000,
        ),
        "UIReceipt": UIReceipt(ui_intent_id="u1", event="displayed", observed_at="2026-07-03T00:00:00Z", source="companion_web"),
        "HotSceneContext": HotSceneContext(session_id="s1", as_of="2026-07-03T00:00:00Z"),
        "EvidenceEvent": EvidenceEvent(
            event_type="object_seen", occurred_at="2026-07-03T00:00:00Z", session_id="s1",
            observation={"label": "phone"}, truth_level="observed", confidence=0.9,
            provenance={"models": ["yolo"]},
        ),
    }


def main() -> int:
    try:
        samples = _samples()
    except Exception as exc:  # pragma: no cover
        print(f"[FAIL] could not construct contract samples: {exc}")
        return 1

    failures = 0
    for name, model in samples.items():
        try:
            payload = model.model_dump_json()
            restored = type(model).model_validate_json(payload)
            back = restored.model_dump()
            if back != model.model_dump():
                raise ValueError("round-trip mismatch")
            json.loads(payload)  # ensure valid JSON
            print(f"[OK]   contract {name} round-trip")
        except Exception as exc:
            failures += 1
            print(f"[FAIL] contract {name}: {exc}")
    if failures:
        print(f"[FAIL] {failures}/{len(samples)} contracts failed round-trip")
        return 1
    print(f"[OK]   all {len(samples)} contracts round-trip cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
