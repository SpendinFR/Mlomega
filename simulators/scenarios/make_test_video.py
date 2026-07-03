from __future__ import annotations

"""Generate a small synthetic test scenario: MP4 + pose JSONL.

Produces a short H.264 MP4 (moving shapes + frame-number text) and a matching
pose trajectory JSONL, both light enough to commit (<2 MB). Used by the WebRTC
transport tests and the bench as a deterministic, hardware-free stand-in for a
real XR capture.

Run: ``python simulators/scenarios/make_test_video.py``
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from packages.contracts.python.models import FrameEnvelope, Pose

try:
    import cv2
except Exception as exc:  # pragma: no cover
    raise RuntimeError("opencv-python-headless is required for make_test_video") from exc

SCENARIO_DIR = Path(__file__).resolve().parent
DEFAULT_MP4 = SCENARIO_DIR / "test_scene.mp4"
DEFAULT_POSE = SCENARIO_DIR / "test_scene_pose.jsonl"


def _frame(idx: int, width: int, height: int) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 200, width, dtype=np.uint8)
    frame[:, :, 1] = (idx * 4) % 256
    box = 90
    cx = int((np.sin(idx / 10.0) * 0.5 + 0.5) * (width - box))
    cy = int((np.cos(idx / 8.0) * 0.5 + 0.5) * (height - box))
    cv2.rectangle(frame, (cx, cy), (cx + box, cy + box), (0, 0, 255), -1)
    cv2.circle(frame, (width // 2, height // 2), 40 + (idx % 30), (0, 255, 255), 3)
    cv2.putText(
        frame,
        f"MLOmega V19 f{idx:03d}",
        (20, height - 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (255, 255, 255),
        2,
    )
    return frame


def make_video(
    *,
    mp4_path: Path = DEFAULT_MP4,
    pose_path: Path = DEFAULT_POSE,
    frames: int = 90,
    fps: int = 30,
    width: int = 1280,
    height: int = 720,
    session_id: str = "scenario-test",
) -> dict[str, object]:
    mp4_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"avc1")
    writer = cv2.VideoWriter(str(mp4_path), fourcc, fps, (width, height))
    if not writer.isOpened():  # fall back to mp4v if H.264 encoder is unavailable
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(mp4_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Could not open VideoWriter for {mp4_path}")

    envelopes: list[FrameEnvelope] = []
    for idx in range(frames):
        img = _frame(idx, width, height)
        writer.write(img)
        t = idx / fps
        envelopes.append(
            FrameEnvelope(
                session_id=session_id,
                frame_id=f"{session_id}-frame-{idx:06d}",
                capture_monotonic_ns=int(t * 1_000_000_000),
                captured_at_utc=datetime.now(timezone.utc).isoformat(),
                pose=Pose(
                    position=[float(np.sin(t)), 0.1 * idx / frames, float(np.cos(t))],
                    rotation=[0.0, 0.0, 0.0, 1.0],
                ),
                rotation=0,
                source="scenario_generator",
            )
        )
    writer.release()

    pose_path.write_text(
        "\n".join(json.dumps({"pose": e.pose.model_dump()}) for e in envelopes) + "\n",
        encoding="utf-8",
    )
    return {
        "mp4": str(mp4_path),
        "pose_jsonl": str(pose_path),
        "frames": frames,
        "mp4_bytes": mp4_path.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=90)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()
    info = make_video(frames=args.frames, fps=args.fps, width=args.width, height=args.height)
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
