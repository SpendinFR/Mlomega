import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.memory


def test_v19_visual_schema_and_direct_evidence_source(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))

    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.v19_visual_store import ensure_v19_visual_schema, store_visual_event
    from mlomega_audio_elite.v18_life_model import _DIRECT_EVIDENCE_SOURCES, _owned_evidence_ref, validate_stratum_evidence

    ensure_v19_visual_schema(db_path)
    event_id = store_visual_event({
        "memory_owner_id": "person-a",
        "live_session_id": "xr-session-a",
        "event_type": "object_seen",
        "occurred_at": "2026-07-03T10:00:00+00:00",
        "entity": {"kind": "object", "label": "phone"},
        "observation": {"state": "on desk"},
        "truth_level": "observed",
        "confidence": 0.92,
        "evidence": [{"frame_id": "frame-1", "sha256": "abc", "kind": "keyframe"}],
        "provenance": {"models": ["simulator"]},
    }, db_path=db_path)

    assert _DIRECT_EVIDENCE_SOURCES["visual_events_v19"] == ("visual_event_id", "person_id", ("occurred_at", "created_at"))
    with connect(db_path) as con:
        ref = _owned_evidence_ref(con, person_id="person-a", table="visual_events_v19", source_id=event_id)
    assert ref["source_table"] == "visual_events_v19"
    assert ref["occurred_at"].startswith("2026-07-03T10:00:00")
    validate_stratum_evidence(refs=[ref], stratum="situational")


def test_v19_visual_store_requires_explicit_owner(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))

    from mlomega_audio_elite.v19_visual_store import store_visual_event

    with pytest.raises(ValueError, match="memory_owner_id"):
        store_visual_event({"live_session_id": "s", "event_type": "x"}, db_path=db_path)


def test_v19_api_endpoints_persist_owner_scoped_payloads(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))

    from fastapi.testclient import TestClient
    from mlomega_audio_elite.api import app
    from mlomega_audio_elite.db import connect

    client = TestClient(app)
    visual_payload = {
        "memory_owner_id": "person-api",
        "live_session_id": "xr-api",
        "event_type": "object_seen",
        "occurred_at": "2026-07-03T11:00:00+00:00",
        "entity": {"entity_id": "entity-phone", "kind": "object", "label": "phone"},
        "observation": {"state": "visible"},
        "truth_level": "observed",
        "confidence": 0.91,
        "evidence": [{"frame_id": "frame-api", "sha256": "sha-api", "kind": "keyframe"}],
        "provenance": {"models": ["simulator"]},
    }
    visual = client.post("/ingest/visual-event", json=visual_payload)
    assert visual.status_code == 200
    assert visual.json()["visual_event_id"]

    summary = client.post("/ingest/scene-summary", json={
        "memory_owner_id": "person-api",
        "live_session_id": "xr-api",
        "summary_start": "2026-07-03T11:00:00+00:00",
        "summary_end": "2026-07-03T11:05:00+00:00",
        "place_hint": "desk",
        "map_quality": 0.75,
        "summary": {"entities": ["entity-phone"]},
        "evidence_refs": [{"source_table": "visual_events_v19", "source_id": visual.json()["visual_event_id"]}],
    })
    assert summary.status_code == 200
    assert summary.json()["scene_summary_id"]

    correction = client.post("/memory/correction-visual", json={
        "memory_owner_id": "person-api",
        "live_session_id": "xr-api",
        "occurred_at": "2026-07-03T11:01:00+00:00",
        "entity": {"entity_id": "entity-phone"},
        "observation": {"correction": "not my phone"},
        "confidence": 1.0,
    })
    assert correction.status_code == 200
    assert correction.json()["status"] == "recorded"

    health = client.get("/xr/session-health", params={"memory_owner_id": "person-api", "live_session_id": "xr-api"})
    assert health.status_code == 200
    assert health.json()["ok"] is True

    clip = client.post("/evidence/request-clip", json={
        "memory_owner_id": "person-api",
        "live_session_id": "xr-api",
        "ui_intent_id": "ui-clip",
        "observed_at": "2026-07-03T11:02:00+00:00",
        "local_track_state": {"track_id": "track-1"},
    })
    assert clip.status_code == 200
    assert clip.json()["status"] == "queued"

    missing_owner = client.post("/ingest/visual-event", json={"live_session_id": "xr-api"})
    assert missing_owner.status_code >= 400

    with connect(db_path) as con:
        assert con.execute("SELECT COUNT(*) FROM visual_events_v19 WHERE person_id='person-api'").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM scene_session_summaries_v19 WHERE person_id='person-api'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM ui_interaction_outcomes_v19 WHERE person_id='person-api'").fetchone()[0] == 1
