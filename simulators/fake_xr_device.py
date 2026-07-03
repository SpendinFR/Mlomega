from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from packages.contracts.python.models import FrameEnvelope, Pose


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeXrDevice:
    def __init__(self, *, session_id: str = "sim-session", fps: float = 30.0, frames: int = 90, rotation: int = 0, source: str = "fake_xr_device") -> None:
        self.session_id = session_id
        self.fps = fps
        self.frames = frames
        self.rotation = rotation
        self.source = source

    async def stream(self) -> AsyncIterator[tuple[dict[str, Any], FrameEnvelope]]:
        delay = 1.0 / self.fps if self.fps > 0 else 0
        for idx in range(self.frames):
            envelope = FrameEnvelope(
                session_id=self.session_id,
                frame_id=f"{self.session_id}-frame-{idx:06d}",
                capture_monotonic_ns=time.monotonic_ns(),
                captured_at_utc=_utc(),
                pose=Pose(position=[0.0, 0.0, 0.0], rotation=[0.0, 0.0, 0.0, 1.0]),
                rotation=self.rotation, source=self.source,
            )
            yield {"width": 1280, "height": 720, "format": "bgr24", "index": idx}, envelope
            if delay:
                await asyncio.sleep(delay)


def write_pose_jsonl(path: Path, envelopes: list[FrameEnvelope]) -> None:
    path.write_text("\n".join(e.model_dump_json() for e in envelopes) + "\n", encoding="utf-8")


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--rotation", type=int, choices=[0, 90, 180, 270], default=0)
    parser.add_argument("--jsonl", type=Path)
    args = parser.parse_args()
    device = FakeXrDevice(frames=args.frames, fps=args.fps, rotation=args.rotation)
    envelopes: list[FrameEnvelope] = []
    async for _frame, envelope in device.stream():
        envelopes.append(envelope)
        print(json.dumps({"frame_id": envelope.frame_id, "rotation": envelope.rotation}))
    if args.jsonl:
        write_pose_jsonl(args.jsonl, envelopes)

if __name__ == "__main__":
    asyncio.run(_main())
