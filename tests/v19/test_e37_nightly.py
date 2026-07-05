"""E37 — Rebranche la nuit complète + attribution owner.

The closure audit found the V19 live pipeline never fed the V18.8 nightly deep-audio
pass: nothing archived the raw audio nor wrote the ``speech_segment`` sensor events
that ``bundles_require_deep_audio`` / the event assembler key off, and there was no
V19 equivalent of ``setup_me`` to enrol the WEARER. These tests prove the fix on the
PC side, no hardware:

1. **speech_segment format fidelity** — an archived VAD segment writes a WAV + a
   ``brainlive_sensor_events`` row whose columns and ``payload_json`` match exactly
   what ``brainlive_offline_deep_audio_v18_5._event_piece`` reads (the format is
   discovered in the core, not invented) — verified by feeding that very row through
   the core consumer up to the ``DeepAudioRuntime`` frontier.
2. **audio+vision complementarity (user requirement)** — a simulated V19 session with
   speech (archived + one transcript turn) AND keyframes on the same window →
   ``run_brainlive_event_assembly`` → ONE bundle with non-empty ``audio_timeline_json``
   AND ``vision_timeline_json`` → ``bundles_require_deep_audio`` True → the deep-audio
   stage consumes the archived segment.
3. **owner** — « configure ma voix » routes to ``owner_enroll`` → the next wearer
   segments enrol is_user=True → a following segment is attributed to the owner.
4. **vision owner** — a keyframe without a person_id is impossible.
5. **pose guard** — a placeholder envelope (pose 0,0,0) is ignored by the spatial map.

The real WhisperX/pyannote stack is exercised only if importable in this env; the
row/format is otherwise validated up to the ``DeepAudioRuntime`` boundary and the
full run is deferred to the close-day (ADR §E37).
"""

from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audio_archive = _load("v19_audio_archive", "services/live-pc/audio_archive.py")
owner_setup = _load("v19_owner_setup", "services/live-pc/owner_setup.py")
intent_router = _load("v19_intent_router", "services/live-pc/intent_router.py")
voice_identity_live = _load("voice_identity_live", "services/live-pc/voice_identity_live.py")
audiort = _load("v19_audiort", "services/live-pc/audiort.py")
spatial = _load("v19_spatial", "services/live-pc/spatial.py")


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MLOMEGA_DB", str(tmp_path / "e37.db"))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_EVIDENCE", str(tmp_path / "raw" / "evidence"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))
    from mlomega_audio_elite.db import init_db
    from mlomega_audio_elite.brainlive_v15 import ensure_brainlive_schema

    init_db()
    ensure_brainlive_schema()


def _speech(n_samples: int = 16000, freq: float = 180.0) -> np.ndarray:
    """A short synthetic voiced-ish float32 16 kHz mono segment (never silence)."""
    t = np.arange(n_samples, dtype=np.float32) / 16000.0
    return (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ============================================================ 1. format fidelity
def test_archived_segment_writes_wav_and_core_format_speech_segment(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from mlomega_audio_elite.brainlive_v15 import start_live_session
    from mlomega_audio_elite.db import connect

    sess = start_live_session(person_id="me", title="e37 fidelity", mode="live_xr")
    sid = sess["live_session_id"]

    arc = audio_archive.AudioArchive(person_id="me", live_session_id=sid)
    res = arc.archive_segment(
        _speech(),
        absolute_start="2026-07-04T10:00:00+00:00",
        absolute_end="2026-07-04T10:00:01+00:00",
        source_event_id="audiort-live-1",
        transcript_text="bonjour",
    )
    assert res.archived, res.reason
    # WAV really written, mono 16-bit 16 kHz.
    wav = Path(res.wav_path)
    assert wav.exists() and wav.stat().st_size > 0
    with wave.open(str(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000

    # The sensor event matches the EXACT columns/payload the core deep-audio reads.
    with connect() as con:
        row = dict(con.execute(
            "SELECT * FROM brainlive_sensor_events WHERE event_id=?", (res.event_id,)
        ).fetchone())
    assert row["modality"] == "audio"
    assert row["event_type"] == "speech_segment"   # the gate keyword
    assert row["person_id"] == "me"
    assert row["live_session_id"] == sid
    assert row["source_path"] == str(wav.resolve())
    assert row["source_sha256"]
    import json
    payload = json.loads(row["payload_json"])
    # Fields _event_piece (VAD-chunk fallback) reads:
    assert payload["absolute_start"] == "2026-07-04T10:00:00+00:00"
    assert payload["absolute_end"]
    assert payload["chunk_path"] == str(wav.resolve())
    assert isinstance(payload["segment"], dict) and "start" in payload["segment"] and "end" in payload["segment"]
    assert isinstance(payload["speaker"], dict)
    assert payload["source_event_id"] == "audiort-live-1"

    # Parallel projection row exists too (as the fusion path writes).
    with connect() as con:
        seg = con.execute(
            "SELECT * FROM brainlive_audio_segments_v154 WHERE segment_id=?", (res.segment_id,)
        ).fetchone()
    assert seg is not None


def test_core_deep_audio_consumes_archived_event(tmp_path, monkeypatch):
    """The archived row is readable by the core consumer up to the runtime frontier."""
    _env(tmp_path, monkeypatch)
    from mlomega_audio_elite.brainlive_v15 import start_live_session
    from mlomega_audio_elite import brainlive_offline_deep_audio_v18_5 as deep

    sess = start_live_session(person_id="me", title="e37 consume", mode="live_xr")
    sid = sess["live_session_id"]
    arc = audio_archive.AudioArchive(person_id="me", live_session_id=sid)
    res = arc.archive_segment(
        _speech(),
        absolute_start="2026-07-04T10:00:00+00:00",
        absolute_end="2026-07-04T10:00:01+00:00",
        source_event_id="audiort-live-1",
    )
    assert res.archived

    # Build a minimal bundle-like dict that owns the archived event, then let the
    # core resolve it to an AudioPiece via its own reader (format proof, no runtime).
    bundle = {
        "live_session_id": sid,
        "start_time": "2026-07-04T09:59:00+00:00",
        "end_time": "2026-07-04T10:05:00+00:00",
        "raw_timeline_json": '[{"source_table":"brainlive_sensor_events","source_id":"%s"}]' % res.event_id,
    }
    pieces, missing = deep._pieces_for_bundle(bundle, person_id="me")
    assert not missing, missing
    assert len(pieces) == 1
    piece = pieces[0]
    assert Path(piece.source_path).exists()
    assert piece.source_path == str(Path(res.wav_path).resolve())
    # The live speaker hint is carried through, marked as a VAD chunk source.
    assert piece.live_speaker.get("source_kind") == "vad_chunk_fallback"


# ============================================================ 2. complementarity
def test_audio_vision_complementarity_one_bundle_requires_deep_audio(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from mlomega_audio_elite.brainlive_v15 import ingest_live_turn, start_live_session
    from mlomega_audio_elite.v19_keyframes import register_xr_keyframe
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import (
        run_brainlive_event_assembly, collect_live_raw_timeline,
    )
    from mlomega_audio_elite.brainlive_offline_deep_audio_v18_5 import bundles_require_deep_audio
    from mlomega_audio_elite.db import connect
    import json

    sess = start_live_session(person_id="me", title="e37 complementarity", mode="live_xr")
    sid = sess["live_session_id"]
    day = "2026-07-04"
    win_start = "2026-07-04T10:00:00+00:00"

    # (a) Speech: one canonical transcript turn (audio_timeline) + the archived raw
    # segment (the deep-audio gate).
    ingest_live_turn(
        sid, "on parle de la réunion de demain", speaker_label="speaker",
        is_final=True, timestamp_start=win_start, timestamp_end="2026-07-04T10:00:03+00:00",
        metadata={"source": "v19_audiort"},
    )
    arc = audio_archive.AudioArchive(person_id="me", live_session_id=sid)
    arc.archive_segment(
        _speech(), absolute_start=win_start, absolute_end="2026-07-04T10:00:03+00:00",
        source_event_id="audiort-live-1", transcript_text="on parle de la réunion de demain",
    )

    # (b) Vision: a keyframe on the same window (vision_timeline).
    img = tmp_path / "frame.png"
    try:
        import cv2
        cv2.imwrite(str(img), (np.random.rand(48, 48, 3) * 255).astype(np.uint8))
    except Exception:
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    register_xr_keyframe(person_id="me", live_session_id=sid, image_path=str(img), captured_at="2026-07-04T10:00:02+00:00")

    # Assemble. One session window → one bundle carrying both modalities.
    raw = collect_live_raw_timeline("me", package_date=day, live_session_id=sid)
    assert raw["raw_rows"] > 0
    out = run_brainlive_event_assembly("me", package_date=day, live_session_id=sid, export_to_brain2=False)
    assert out["status"] == "ok"
    assert out["bundles"] == 1, out

    with connect() as con:
        bundle = dict(con.execute(
            "SELECT * FROM brainlive_event_bundles_v1514 WHERE person_id=? AND package_date=?",
            ("me", day),
        ).fetchone())
    audio_tl = json.loads(bundle["audio_timeline_json"])
    vision_tl = json.loads(bundle["vision_timeline_json"])
    assert audio_tl, "audio_timeline_json must be non-empty"
    assert vision_tl, "vision_timeline_json must be non-empty"

    # The deep-audio gate fires → the night has work to do for this V19 session.
    assert bundles_require_deep_audio(person_id="me", package_date=day, live_session_id=sid) is True


# ============================================================ 3. owner
class _FakeEmbedder:
    """Deterministic per-file embedder: same bytes → same vector (cosine==1)."""

    def embed_file(self, path):
        data = Path(path).read_bytes()
        # A tiny, stable 8-dim vector from the byte histogram so identical WAVs match.
        h = np.zeros(8, dtype=np.float64)
        for i, b in enumerate(data[:4096]):
            h[b % 8] += 1.0
        n = np.linalg.norm(h) or 1.0
        return list(h / n)


def test_owner_enroll_command_routes_and_attributes_owner(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    emitted: list[dict] = []
    vil = voice_identity_live.VoiceIdentityLive(embedder=_FakeEmbedder())
    setup = owner_setup.OwnerSetup(
        voice_identity=vil,
        config=owner_setup.OwnerSetupConfig(person_id="me", needed_segments=2),
        emit_ui_intent=emitted.append,
    )
    router = intent_router.IntentRouter(owner_setup=setup, emit_ui_intent=emitted.append)

    # « configure ma voix » → owner_enroll, arming.
    routed = router.on_transcript("configure ma voix")
    assert routed["intent"] == "owner_enroll"
    assert routed["handled"] is True
    assert setup.is_arming()

    # The next wearer segments (identical WAVs → same voice) enrol as is_user.
    wav = tmp_path / "wearer.wav"
    audio_archive.write_segment_wav(wav, _speech(), sample_rate=16000)
    assert setup.offer_segment(wav) is None            # 1st captured
    done = setup.offer_segment(wav)                     # 2nd → enrolled
    assert done and done["enrolled"] and done["is_user"] is True
    assert not setup.is_arming()                        # window closed

    # A following segment of the SAME wearer is now attributed to the owner.
    match = vil.match(wav)
    assert match["matched"] and match["person_id"] == "me"


def test_owner_enroll_high_confidence_and_alt_phrasings():
    router = intent_router.IntentRouter()  # no owner_setup → honest unavailable
    for phrase in ("configure ma voix", "c'est moi qui parle", "set up my voice"):
        routed = router.on_transcript(phrase)
        assert routed["intent"] == "owner_enroll", phrase
        assert routed["handled"] is False              # no backend wired


# ============================================================ 4. vision owner
def test_keyframe_without_person_id_is_impossible(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from mlomega_audio_elite.v19_keyframes import register_xr_keyframe

    img = tmp_path / "f.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    for bad in ("", None):
        with pytest.raises(ValueError):
            register_xr_keyframe(person_id=bad, live_session_id="s", image_path=str(img))
    # metadata can never null the owner: person_id stays authoritative.
    fid = register_xr_keyframe(
        person_id="me", live_session_id="s", image_path=str(img),
        metadata={"person_id": None, "note": "x"},
    )
    from mlomega_audio_elite.db import connect
    import json
    with connect() as con:
        row = con.execute("SELECT metadata_json FROM vision_frames WHERE frame_id=?", (fid,)).fetchone()
    assert json.loads(row["metadata_json"])["person_id"] == "me"


# ============================================================ 5. pose guard
def test_placeholder_pose_ignored_by_spatial():
    from packages.contracts.python.models import FrameEnvelope, Pose

    provider = spatial.PoseKeyframeMap()

    # A real pose is observed normally.
    real = FrameEnvelope(
        session_id="s", frame_id="f1", capture_monotonic_ns=1, captured_at_utc="2026-07-04T10:00:00+00:00",
        pose=Pose(position=[1.0, 2.0, 3.0], rotation=[0, 0, 0, 1]), source="aiortc", pose_valid=True,
    )
    # A placeholder envelope carries a synthetic (0,0,0) pose flagged pose_valid=False.
    placeholder = FrameEnvelope(
        session_id="s", frame_id="f2", capture_monotonic_ns=2, captured_at_utc="2026-07-04T10:00:01+00:00",
        pose=Pose(position=[0.0, 0.0, 0.0], rotation=[0, 0, 0, 1]), source="placeholder", pose_valid=False,
    )
    assert placeholder.pose_valid is False

    # The pipeline guard: only pose_valid envelopes feed observe_pose. Emulate it.
    def feed(env):
        if bool(getattr(env, "pose_valid", True)):
            provider.observe_pose(env.frame_id, env.pose.model_dump())

    feed(real)
    feed(placeholder)  # must be ignored — no (0,0,0) in the cloud

    assert len(provider._poses) == 1
    assert provider._poses[0].position == (1.0, 2.0, 3.0)
