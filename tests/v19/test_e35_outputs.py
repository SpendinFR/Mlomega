"""E35 — Outputs: voice (TTS), replay, correction, generalised hot context.

Real PC-side checks with a temp SQLite DB (no hardware, no cloud):

* **TTS** : a real provider (sherpa if a voice model is present, else the SAPI /
  pyttsx3 fallback behind the SAME interface) synthesises a short reply → valid
  non-empty WAV → a bounded ``tts_audio`` DataChannel message; the viewer-blob
  wrapper caps the base64 payload;
* **replay** : keyframes + events seeded on a time window → the bundle counts them
  → a ``virtual_screen`` UIIntent with the frame refs + a timeline ContextCard;
  « rejoue 14h30 » via the router (LLM frontier mocked) → the bundle of the right
  window;
* **object correction** : « ce n'est pas mon téléphone » → the label is suspended
  in WorldBrain, absent from every subsequent SceneDelta, + a revise_memory trace;
  a place correction clears the zone; a person correction still routes to identity;
* **generalised hot** : a recognised zone → ``spatial_hot_update`` (with a matching
  daily routine) ; a durable object → ``entity_hot_update`` kind=object ; an active
  task → ``task_hot_update``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


worldbrain = _load("v19_worldbrain", "services/live-pc/worldbrain.py")
scene_adapter = _load("v19_scene_adapter", "services/live-pc/brainlive_scene_adapter.py")
enrollment_watcher = _load("v19_enrollment_watcher", "services/live-pc/enrollment_watcher.py")
intent_router = _load("v19_intent_router", "services/live-pc/intent_router.py")
tts_local = _load("v19_tts_local", "services/live-pc/tts_local.py")
replay_service = _load("v19_replay_service", "services/live-pc/replay_service.py")


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))
    return db_path


def _ent(track_id, label, bbox, conf=0.8, kind="object"):
    return {"track_id": track_id, "kind": kind, "label": label, "bbox": bbox,
            "confidence": conf, "visibility": 1.0, "age": 1}


def _delta(frame_id, entities, *, map_quality=0.0):
    return {"session_id": "s-e35", "source_frame_id": frame_id, "entities": entities,
            "relations": [], "changes": [], "map_quality": map_quality,
            "evidence_refs": [f"frame:{frame_id}"]}


def _wb(db_path, session_id="s-e35"):
    return worldbrain.WorldBrain(person_id="me", live_session_id=session_id, db_path=db_path,
                                 publish_world_state=False)


# --------------------------------------------------------------------------- §1 TTS
def test_tts_real_provider_produces_valid_wav_and_bounded_message():
    import io
    import wave

    # A real provider behind the shared interface (sherpa if a voice model was
    # fetched, else the platform SAPI/pyttsx3 fallback). No cloud, no hardware.
    provider = tts_local.build_tts_provider({})
    try:
        wav = provider.speak("Bonjour, ceci est un test.", lang="fr")
    except tts_local.TTSUnavailable:
        pytest.skip("no local TTS provider available in this environment")
    assert wav and wav[:4] == b"RIFF", "a real provider must produce a valid WAV blob"
    # It parses as a mono 16-bit PCM WAV with actual audio frames.
    w = wave.open(io.BytesIO(wav), "rb")
    assert w.getnchannels() == 1 and w.getsampwidth() == 2
    assert w.getnframes() > 0, "the WAV must carry audio, not be empty"

    msg = tts_local.tts_audio_message(wav, lang="fr", text="Bonjour")
    assert msg is not None and msg["type"] == "tts_audio"
    assert msg["format"] == "wav" and msg["audio_b64"], "the message carries base64 WAV"
    # The DataChannel push is bounded: an over-budget reply yields None (text card).
    assert tts_local.tts_audio_message(wav, max_b64_chars=10) is None


def test_tts_profile_toggle_default_off():
    assert tts_local.profile_tts_enabled({}) is False
    assert tts_local.profile_tts_enabled({"tts": "off"}) is False
    assert tts_local.profile_tts_enabled({"tts": "on"}) is True


def test_tts_fallback_interface_is_uniform():
    # Every tier answers the SAME speak(text, lang) -> bytes contract (ADR §E35).
    for cls in (tts_local.SherpaTTS, tts_local.Pyttsx3TTS, tts_local.WindowsSapiTTS):
        assert hasattr(cls, "speak")


# --------------------------------------------------------------------------- §2 replay
def _seed_replay_window(db_path, *, person_id="me", day="2026-07-05", hour="14", minute="32"):
    """Seed a keyframe + a visual event inside the 14h30–14h45 window."""
    from mlomega_audio_elite.brainlive_v15 import ensure_brainlive_schema
    from mlomega_audio_elite.v19_visual_store import ensure_v19_visual_schema, store_visual_event
    from mlomega_audio_elite.db import connect, write_transaction, insert_only
    from mlomega_audio_elite.utils import now_iso

    ensure_brainlive_schema()
    ensure_v19_visual_schema(db_path)
    at = f"{day}T{hour}:{minute}:00+00:00"
    with connect(db_path) as con, write_transaction(con):
        insert_only(con, "vision_frames", {
            "frame_id": "kf1", "live_session_id": "s-e35", "captured_at": at,
            "image_path": "/raw/kf1.jpg", "image_sha256": "aa", "created_at": now_iso(),
        }, on_conflict="ignore")
    store_visual_event({
        "memory_owner_id": person_id, "live_session_id": "s-e35",
        "event_type": "change_appeared", "occurred_at": at,
        "entity": {"label": "perceuse"}, "truth_level": "observed",
        "confidence": 0.7, "evidence": ["frame:kf1"],
    }, db_path=db_path)
    return at


def test_replay_bundle_and_intents_from_seeded_window(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_replay_window(db_path)
    emitted = []
    svc = replay_service.ReplayService(
        person_id="me", live_session_id="s-e35", db_path=db_path, emit_ui_intent=emitted.append,
    )
    res = svc.replay(time="14h30", date="2026-07-05")
    assert res["status"] == "ok"
    counts = res["bundle"]["counts"]
    assert counts["keyframes"] == 1 and counts["events"] == 1, "the seeded window must be assembled"
    # virtual_screen intent carries the frame refs; timeline card summarises.
    vscreen = res["virtual_screen"]
    assert vscreen["component"] == "virtual_screen"
    assert len(vscreen["content"]["frames"]) == 1
    assert res["timeline"]["content"]["kind"] == "replay_timeline"
    # Both intents were pushed to the (companion/device) sink.
    kinds = [i.get("component") for i in emitted]
    assert "virtual_screen" in kinds and "context_card" in kinds


def test_replay_window_excludes_out_of_range(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_replay_window(db_path, hour="14", minute="32")
    svc = replay_service.ReplayService(person_id="me", live_session_id="s-e35", db_path=db_path)
    # A 03h00 window contains nothing.
    res = svc.replay(time="03h00", date="2026-07-05")
    assert res["bundle"]["counts"]["keyframes"] == 0
    assert svc.metrics["empty_windows"] == 1


def test_replay_via_router_llm_frontier_mocked(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_replay_window(db_path)

    class _StubLLM:
        def complete_json(self, system, user, *, schema_hint=None, timeout=None):
            return {"intent": "replay", "time": "14h30"}

    captured = {}
    svc = replay_service.ReplayService(person_id="me", live_session_id="s-e35", db_path=db_path)

    def _wrapped_replay(*, time, date=None):
        res = svc.replay(time=time, date="2026-07-05")
        captured["window"] = res["window"]
        captured["counts"] = res["bundle"]["counts"]
        return res

    class _ReplayProxy:
        def replay(self, *, time, date=None):
            return _wrapped_replay(time=time, date=date)

    router = intent_router.IntentRouter(llm_router=_StubLLM(), replay_service=_ReplayProxy())
    out = router.on_transcript("tu peux me montrer ce que j'ai fait vers 14h30 ?")
    assert out["intent"] == "replay" and out["handled"] is True
    # The bundle is for the right window and picked up the seeded keyframe.
    assert captured["counts"]["keyframes"] == 1
    assert captured["window"]["start"].startswith("2026-07-05T14:30")


# --------------------------------------------------------------------------- §3 correction
def test_object_correction_suspends_label_end_to_end(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    wb = _wb(db_path)
    for f in ("a", "b", "c"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "téléphone", [10, 10, 60, 80])]))
    assert any(e["label"] == "téléphone" for e in wb.snapshot()["entities"]), "seeded object is present"

    watcher = enrollment_watcher.EnrollmentWatcher(worldbrain=wb, person_id="me")
    res = watcher.on_transcript("ce n'est pas mon téléphone")
    assert res is not None and res["intent"] == "correct_object"
    assert res["suspended"] is True and res["hidden"] == 1
    # The suspended label is absent from the snapshot AND does not reappear on the
    # NEXT SceneDeltas (never re-promotes).
    assert not any(e["label"] == "téléphone" for e in wb.snapshot()["entities"])
    for f in ("d", "e", "f", "g"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "téléphone", [10, 10, 60, 80])]))
    assert not any(e["label"] == "téléphone" for e in wb.snapshot()["entities"]), \
        "a corrected label must not resurface in subsequent SceneDeltas"


def test_object_correction_records_revise_memory(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    # Seed an atomic memory mentioning the object so revise_memory has a target.
    from mlomega_audio_elite.db import connect, write_transaction, init_db
    from mlomega_audio_elite.utils import now_iso
    init_db(db_path)
    with connect(db_path) as con, write_transaction(con):
        cols = [r[1] for r in con.execute("PRAGMA table_info(atomic_memories)").fetchall()]
    if "memory_id" not in cols or "content" not in cols:
        pytest.skip("atomic_memories schema not available in this build")
    with connect(db_path) as con, write_transaction(con):
        con.execute(
            "INSERT INTO atomic_memories(memory_id, kind, person_id, content, created_at) VALUES(?,?,?,?,?)",
            ("m-tel", "fact", "me", "mon téléphone est un Pixel", now_iso()),
        )
    wb = _wb(db_path)
    for f in ("a", "b", "c"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "téléphone", [10, 10, 60, 80])]))
    watcher = enrollment_watcher.EnrollmentWatcher(worldbrain=wb, person_id="me")
    res = watcher.on_transcript("ce n'est pas mon téléphone")
    assert res["memory_revision"] is not None, "a matching memory must be revised (invalidated)"


def test_place_correction_clears_zone(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    wb = _wb(db_path)
    wb.session.place_hint = "bureau"
    wb.session.active_zone = "bureau"
    watcher = enrollment_watcher.EnrollmentWatcher(worldbrain=wb, person_id="me")
    res = watcher.on_transcript("on n'est pas au bureau")
    assert res is not None and res["intent"] == "correct_place" and res["suspended"] is True
    assert wb.session.place_hint is None and wb.session.active_zone is None


def test_person_correction_still_routes_to_identity(tmp_path, monkeypatch):
    # "ce n'est pas Paul" must be a PERSON correction, not a scene correction.
    db_path = _env(tmp_path, monkeypatch)
    assert enrollment_watcher.parse_scene_correction("ce n'est pas Paul") is None
    cmd = enrollment_watcher.parse_identity_command("ce n'est pas Paul")
    assert cmd is not None and cmd["intent"] == "correct" and cmd["name"] == "Paul"


# --------------------------------------------------------------------------- §4 hot
def _seed_routine(db_path, *, person_id="me", place_key="bureau", entity_key="café"):
    from mlomega_audio_elite.v19_visual_store import ensure_v19_visual_schema
    from mlomega_audio_elite.db import connect, write_transaction, upsert
    from mlomega_audio_elite.utils import now_iso

    ensure_v19_visual_schema(db_path)
    now = now_iso()
    with connect(db_path) as con, write_transaction(con):
        upsert(con, "brain2_spatial_routine_models", {
            "routine_id": "r1", "person_id": person_id, "entity_key": entity_key,
            "place_key": place_key, "time_slot": "morning", "occurrence_count": 5,
            "confidence": 0.8, "updated_at": now, "created_at": now,
        }, "routine_id")


def test_generalised_hot_updates_all_four_types(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    _seed_routine(db_path, place_key="bureau")
    wb = _wb(db_path)
    emitted = []
    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-e35", worldbrain=wb, db_path=db_path,
        on_entity_hot_update=emitted.append,
    )
    # A durable object in a recognised zone, and an active task.
    for f in ("a", "b", "c"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "perceuse", [10, 10, 60, 80])], map_quality=0.7))
    wb.session.active_zone = "bureau"
    wb.session.place_hint = "bureau"
    adapter.set_active_task({"task_key": "bricolage", "goal": "monter l'étagère",
                             "next_step": "percer", "tools": ["perceuse"]})
    adapter.evaluate_situations()

    by_type: dict[str, list] = {}
    for m in emitted:
        by_type.setdefault(m["type"], []).append(m)

    # (a) spatial_hot_update with the recognised zone AND the matching daily routine.
    spatial = by_type.get("spatial_hot_update") or []
    assert spatial, "a recognised zone must push a spatial_hot_update"
    assert spatial[0]["zone"] == "bureau"
    assert spatial[0]["routines"] and spatial[0]["routines"][0]["place_key"] == "bureau", \
        "the daily routine of the place must be included (§4d)"

    # (b) entity_hot_update kind=object for the durable object.
    objects = [m for m in by_type.get("entity_hot_update", []) if m.get("kind") == "object"]
    assert objects and objects[0]["label"] == "perceuse"

    # (c) task_hot_update for the active task.
    tasks = by_type.get("task_hot_update") or []
    assert tasks and tasks[0]["goal"] == "monter l'étagère" and tasks[0]["step"] == "percer"

    assert adapter.metrics["spatial_hot_updates"] == 1
    assert adapter.metrics["object_hot_updates"] >= 1
    assert adapter.metrics["task_hot_updates"] == 1


def test_generalised_hot_updates_are_deduped_per_session(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    wb = _wb(db_path)
    emitted = []
    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-e35", worldbrain=wb, db_path=db_path,
        on_entity_hot_update=emitted.append,
    )
    for f in ("a", "b", "c"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "perceuse", [10, 10, 60, 80])]))
    wb.session.active_zone = "bureau"
    adapter.evaluate_situations()
    n_first = len(emitted)
    assert n_first >= 2  # at least spatial + object
    # A second pass over the same scene must not re-emit (one push per subject).
    adapter.evaluate_situations()
    assert len(emitted) == n_first, "hot updates are deduped per subject per session"


def test_person_hot_update_still_object_free(tmp_path, monkeypatch):
    # A person entity must NOT be pushed by the object path (persons ride E34's
    # prefetch_relation_pack instead).
    db_path = _env(tmp_path, monkeypatch)
    wb = _wb(db_path)
    emitted = []
    adapter = scene_adapter.BrainLiveSceneAdapter(
        person_id="me", live_session_id="s-e35", worldbrain=wb, db_path=db_path,
        on_entity_hot_update=emitted.append,
    )
    for f in ("a", "b", "c"):
        wb.ingest_scene_delta(_delta(f, [_ent("t1", "person", [10, 10, 60, 80], kind="object")]))
    adapter.evaluate_situations()
    objects = [m for m in emitted if m.get("type") == "entity_hot_update" and m.get("kind") == "object"]
    assert not objects, "a person is not pushed via the object hot path"
