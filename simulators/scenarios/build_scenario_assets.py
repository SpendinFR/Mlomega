from __future__ import annotations

"""Build reproducible synthetic assets for the 16 E29 scenarios.

Extends ``make_test_video.py`` with a per-scenario asset builder. Every asset is
seeded and deterministic so a scenario reruns identically. Three primitives keep
the assets *real* against the actual pipeline (no stubs):

* **person frames** — composited from the detector-verified ``fixtures/people.jpg``
  (YOLOX-nano reliably reports ``person`` on it), so any scenario that needs a
  person track gets a genuine detection, not a hand-drawn shape the detector
  would miss;
* **readable text** — synthetic high-contrast captions that RapidOCR reads back
  (verified GATE/BOARDING style), for the OCR / zoom / "what is this" scenarios;
* **pose traces** — JSONL trajectories (dense & smooth → the spatial provider's
  ``map_quality`` qualifies; sparse/jittery → it does not), for last-seen /
  bearing / navigation scenarios.

WAV audio reuses the E27 TTS fixtures (``fixtures/speech_en.wav`` /
``speech_fr.wav``) when present — the translation / conversational scenarios feed
those straight into the real AudioRT.

Run: ``python simulators/scenarios/build_scenario_assets.py`` (writes into
``simulators/scenarios/assets/`` and prints a JSON summary). The runner
(``scripts/run_scenarios_v19.py``) calls :func:`build_all` lazily so assets are
generated on demand and never need committing.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from packages.contracts.python.models import FrameEnvelope, Pose  # noqa: E402

try:
    import cv2
except Exception as exc:  # pragma: no cover
    raise RuntimeError("opencv-python-headless is required for build_scenario_assets") from exc

SCENARIO_DIR = Path(__file__).resolve().parent
ASSET_DIR = SCENARIO_DIR / "assets"
FIXTURE_DIR = _ROOT / "tests" / "v19" / "fixtures"
PEOPLE_JPG = FIXTURE_DIR / "people.jpg"
SPEECH_EN = FIXTURE_DIR / "speech_en.wav"
SPEECH_FR = FIXTURE_DIR / "speech_fr.wav"

WIDTH, HEIGHT = 1280, 720


# --------------------------------------------------------------------------- base frames
def _person_base() -> np.ndarray | None:
    """A detector-verified person frame (or None if the fixture is missing)."""
    if not PEOPLE_JPG.exists():
        return None
    img = cv2.imread(str(PEOPLE_JPG))
    if img is None:
        return None
    if img.shape[1] != WIDTH or img.shape[0] != HEIGHT:
        img = cv2.resize(img, (WIDTH, HEIGHT))
    return img


def _blank(rng: np.random.Generator) -> np.ndarray:
    """A neutral room-like background (never all-black, so H.264 has structure)."""
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(40, 120, WIDTH, dtype=np.uint8)
    frame[:, :, 1] = np.linspace(60, 100, WIDTH, dtype=np.uint8)[::-1]
    frame[:, :, 2] = 70
    return frame


def _put_text(frame: np.ndarray, text: str, org: tuple[int, int], scale: float = 1.4) -> None:
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 6)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2)


def _text_panel(frame: np.ndarray, lines: list[str], x: int = 60, y0: int = 180) -> None:
    """White panel with high-contrast text RapidOCR reads reliably."""
    h = 90 * len(lines) + 60
    cv2.rectangle(frame, (x - 20, y0 - 70), (x + 760, y0 - 70 + h), (245, 245, 245), -1)
    for i, ln in enumerate(lines):
        cv2.putText(frame, ln, (x, y0 + i * 90), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (10, 10, 10), 3)


def _object_box(frame: np.ndarray, cx: int, cy: int, color: tuple[int, int, int], size: int = 90) -> None:
    cv2.rectangle(frame, (cx, cy), (cx + size, cy + size), color, -1)
    cv2.rectangle(frame, (cx, cy), (cx + size, cy + size), (255, 255, 255), 2)


# --------------------------------------------------------------------------- writer
def _write_video(path: Path, frames: list[np.ndarray], fps: int = 15) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mp4v first: the in-process runner reads these back with OpenCV, and the
    # avc1/openh264 encoder is often absent on CI/dev boxes (noisy failure). The
    # --webrtc path re-encodes to H.264 in aiortc, so a portable container here is
    # enough for a reproducible, committable-free asset.
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (WIDTH, HEIGHT))
    if not writer.isOpened():
        fourcc = cv2.VideoWriter_fourcc(*"avc1")
        writer = cv2.VideoWriter(str(path), fourcc, fps, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise RuntimeError(f"cannot open VideoWriter for {path}")
    for f in frames:
        writer.write(f)
    writer.release()


def _write_pose(path: Path, poses: list[Pose], session_id: str, fps: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    envs = []
    for idx, pose in enumerate(poses):
        envs.append(
            FrameEnvelope(
                session_id=session_id,
                frame_id=f"{session_id}-frame-{idx:06d}",
                capture_monotonic_ns=int(idx / fps * 1e9),
                captured_at_utc=datetime.now(timezone.utc).isoformat(),
                pose=pose,
                rotation=0,
                source="scenario_generator",
            )
        )
    path.write_text("\n".join(json.dumps({"pose": e.pose.model_dump()}) for e in envs) + "\n", encoding="utf-8")


def _smooth_poses(n: int, jitter: float = 0.0, seed: int = 0) -> list[Pose]:
    """Dense smooth trajectory (map qualifies). jitter>0 scatters it (map fails)."""
    rng = np.random.default_rng(seed)
    out: list[Pose] = []
    for i in range(n):
        t = i / max(1, n)
        base = [float(np.sin(t * 0.5)) * 0.2, 0.0, float(np.cos(t * 0.5)) * 0.2]
        if jitter:
            base = [b + float(rng.normal(0, jitter)) for b in base]
        out.append(Pose(position=base, rotation=[0.0, 0.0, 0.0, 1.0]))
    return out


# --------------------------------------------------------------------------- scenario builders
def _rotate90(frame: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(np.rot90(frame))


def _jitter(f: np.ndarray, i: int) -> np.ndarray:
    """Small deterministic per-frame shift so the adaptive cadence keeps running
    the detector (a perfectly static image floors the detector to fps_min)."""
    dx = (i % 5) - 2
    dy = ((i // 5) % 3) - 1
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(f, M, (f.shape[1], f.shape[0]), borderMode=cv2.BORDER_REPLICATE)


def _build_person_scene(rng: np.random.Generator, frames: int, *, caption: str | None = None) -> list[np.ndarray]:
    base = _person_base()
    out = []
    for i in range(frames):
        f = base.copy() if base is not None else _blank(rng)
        if base is None:
            # fallback: a person-shaped column (best-effort; person fixture preferred)
            cv2.rectangle(f, (560, 200), (720, 680), (60, 60, 200), -1)
        f = _jitter(f, i)
        if caption:
            _put_text(f, caption, (40, 60), scale=1.0)
        out.append(f)
    return out


def _build_text_scene(rng: np.random.Generator, frames: int, lines: list[str]) -> list[np.ndarray]:
    out = []
    for i in range(frames):
        f = _blank(rng)
        _text_panel(f, lines)
        out.append(f)
    return out


def _build_moved_object_scene(rng: np.random.Generator, frames: int) -> list[np.ndarray]:
    """A real detectable subject present, then absent (last-seen / disappeared).

    Uses the detector-verified person fixture as the subject so WorldBrain forms a
    real entity and then marks it last-seen when it leaves — the Sherlock / find /
    changes chains need a genuine ChangeEvent, not an undetectable drawn box.
    """
    base = _person_base()
    out = []
    half = max(2, frames // 2)
    for i in range(frames):
        if i < half and base is not None:
            f = _jitter(base.copy(), i)  # subject present (moving slightly → detector runs)
        else:
            # subject gone → strong full-frame motion so the adaptive cadence stays
            # at fps_max and the detector runs on the (now empty) scene, producing
            # the empty-entity SceneDeltas that trigger the disappearance change.
            f = _blank(rng)
            shift = ((i * 37) % 200)
            f[:, :, 1] = (f[:, :, 1].astype(int) + shift) % 256
            _object_box(f, 200 + shift, 200 + (i * 23) % 300, (40, 160, 40))
        out.append(f)
    return out


# name → builder spec. Each returns (frames, pose_kind, wav, rotation).
def _scene_frames(name: str, rng: np.random.Generator, frames: int) -> tuple[list[np.ndarray], list[Pose], Path | None, int]:
    dense = _smooth_poses(frames, jitter=0.0, seed=1)
    sparse = _smooth_poses(frames, jitter=0.5, seed=2)

    if name in ("life_memory", "person_profile", "conversational", "known_person", "free_guy"):
        return _build_person_scene(rng, frames, caption=None), dense, SPEECH_EN if SPEECH_EN.exists() else None, 0
    if name == "translation":
        # French speech + target English → source≠target → real translation path.
        return _build_person_scene(rng, frames), dense, SPEECH_FR if SPEECH_FR.exists() else None, 0
    if name in ("what_is_this", "zoom_ocr"):
        return _build_text_scene(rng, frames, ["GATE 12", "BOARDING NOW", "SEAT 24A"]), dense, None, 0
    if name in ("find_object", "navigation"):
        return _build_moved_object_scene(rng, frames), dense, None, 0
    if name == "worldbrain_changes" or name == "sherlock":
        return _build_moved_object_scene(rng, frames), dense, None, 0
    if name == "replay":
        return _build_person_scene(rng, frames), dense, SPEECH_EN if SPEECH_EN.exists() else None, 0
    if name == "floating_screen":
        return _build_text_scene(rng, frames, ["NOTES", "VIRTUAL SCREEN"]), dense, None, 0
    if name == "ultra_live_reflex":
        # object growing fast in view → proximity cue
        out = []
        for i in range(frames):
            f = _blank(rng)
            s = 40 + i * 8
            _object_box(f, WIDTH // 2 - s // 2, HEIGHT // 2 - s // 2, (0, 0, 220), size=s)
            out.append(f)
        return out, sparse, None, 0
    if name == "capture_only":
        base = _build_text_scene(rng, frames, ["EXIT 4B", "TERMINAL 2"])
        return [_rotate90(f) for f in base], dense, None, 90
    if name == "assist_task":
        return _build_text_scene(rng, frames, ["STEP 1", "OPEN VALVE"]), dense, None, 0
    # default
    return _build_person_scene(rng, frames), dense, None, 0


def build_one(name: str, *, frames: int = 24, fps: int = 15, seed: int = 0) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    session_id = f"scn-{name}"
    frame_list, poses, wav, rotation = _scene_frames(name, rng, frames)
    # capture_only stores rotated frames → dims differ; write at those dims.
    mp4 = ASSET_DIR / f"{name}.mp4"
    pose_path = ASSET_DIR / f"{name}_pose.jsonl"
    if rotation == 90:
        # rotated frames are HxW swapped; write with a dedicated writer.
        _write_rotated_video(mp4, frame_list, fps)
    else:
        _write_video(mp4, frame_list, fps)
    _write_pose(pose_path, poses, session_id, fps)
    return {
        "name": name,
        "mp4": str(mp4),
        "pose_jsonl": str(pose_path),
        "wav": str(wav) if wav else None,
        "rotation": rotation,
        "frames": len(frame_list),
        "fps": fps,
        "session_id": session_id,
    }


def _write_rotated_video(path: Path, frames: list[np.ndarray], fps: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    h, w = frames[0].shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    if not writer.isOpened():
        fourcc = cv2.VideoWriter_fourcc(*"avc1")
        writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    for f in frames:
        writer.write(f)
    writer.release()


ALL_SCENARIOS = [
    "life_memory", "person_profile", "conversational", "translation",
    "what_is_this", "zoom_ocr", "assist_task", "find_object", "navigation",
    "worldbrain_changes", "sherlock", "replay", "free_guy", "floating_screen",
    "ultra_live_reflex", "capture_only",
]


def build_all(*, frames: int = 24, fps: int = 15, seed: int = 42) -> dict[str, Any]:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {}
    for i, name in enumerate(ALL_SCENARIOS):
        out[name] = build_one(name, frames=frames, fps=fps, seed=seed + i)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--scenario", type=str, default=None, help="build a single scenario")
    args = parser.parse_args()
    if args.scenario:
        info = {args.scenario: build_one(args.scenario, frames=args.frames, fps=args.fps)}
    else:
        info = build_all(frames=args.frames, fps=args.fps)
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
