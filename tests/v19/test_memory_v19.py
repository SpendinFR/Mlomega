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
