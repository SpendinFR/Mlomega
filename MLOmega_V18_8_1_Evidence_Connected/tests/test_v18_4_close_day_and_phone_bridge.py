from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_RAW", str(root / "raw"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")
    return root


def test_close_day_runs_all_stages_and_requires_post_stop_gate(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.v18_close_day as close
    import mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 as post
    import mlomega_audio_elite.brain2_longitudinal_cases_v17 as longitudinal
    import mlomega_audio_elite.brainlive_brain2_coordination_v15_12 as coordination
    import mlomega_audio_elite.brain2_life_model_updater_v15_13 as life
    import mlomega_audio_elite.brainlive_personal_model_v15_9 as live_ready
    from mlomega_audio_elite.db import connect

    monkeypatch.setattr(post, "post_stop_cleanup_eligible", lambda **_kw: {"eligible": True, "status": "completed"})
    monkeypatch.setattr(longitudinal, "run_longitudinal_consolidation", lambda **_kw: {"status": "completed", "run_id": "long-1"})
    monkeypatch.setattr(coordination, "run_brainlive_brain2_coordination", lambda *_a, **_kw: {"status": "ok", "run_id": "coord-1"})
    monkeypatch.setattr(life, "run_brain2_life_model_update", lambda *_a, **_kw: {"status": "llm_patch_ready", "patch_run_id": "life-1"})
    monkeypatch.setattr(live_ready, "build_brain2_live_personal_model", lambda **_kw: {"status": "active", "export_id": "ready-1"})

    first = close.close_brainlive_day(
        person_id="me",
        package_date="2026-06-21",
        post_stop_result={"status": "completed", "run_id": "post-1"},
    )
    assert first["status"] == "completed"
    assert first["cleanup"]["eligible"] is True
    assert set(first["stages"]) == {"post_stop", "longitudinal", "coordination", "life_model", "live_ready"}

    second = close.close_brainlive_day(person_id="me", package_date="2026-06-21")
    assert second["status"] == "completed"
    assert second["resumed_close_day"] is True

    with connect() as con:
        rows = [dict(r) for r in con.execute("SELECT status,cleanup_eligible FROM v18_close_day_runs")]
        stages = [dict(r) for r in con.execute("SELECT stage_name,status FROM v18_pipeline_stages ORDER BY stage_name")]
    assert rows == [{"status": "completed", "cleanup_eligible": 1}]
    assert [row["status"] for row in stages] == ["completed"] * 5


def test_close_day_never_marks_cleanup_eligible_when_post_stop_gate_fails(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.v18_close_day as close
    import mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 as post

    monkeypatch.setattr(post, "post_stop_cleanup_eligible", lambda **_kw: {"eligible": False, "reason": "missing_manifest"})
    result = close.close_brainlive_day(
        person_id="me",
        package_date="2026-06-22",
        post_stop_result={"status": "completed", "run_id": "post-unsafe"},
    )
    assert result["status"] == "blocked"
    assert result["cleanup"]["eligible"] is False
    assert "post-stop cleanup gate" in result["error"]


def test_phone_receiver_collapses_retried_source_event(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("MLOMEGA_PROJECT_ROOT", str(project))
    monkeypatch.setenv("MLOMEGA_PHONE_TOKEN", "test-token")
    candidates = [
        Path(os.environ.get("MLOMEGA_PHONE_BRIDGE_PATH", "")) / "pc" / "brainlive_phone_receiver.py",
        Path(__file__).resolve().parents[1] / "MLOmega_Phone_Bridge_V18_8" / "pc" / "brainlive_phone_receiver.py",
        Path("/mnt/data/mlomega_phone_bridge_v18_4/pc/brainlive_phone_receiver.py"),
    ]
    module_path = next((candidate for candidate in candidates if candidate.exists()), None)
    assert module_path is not None, "Phone Bridge source is required for this integration test"
    name = f"bridge_v185_{os.getpid()}_{id(project)}"
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    sys.modules[name] = bridge
    spec.loader.exec_module(bridge)

    first_blob = project / "first.jpg"
    retry_blob = project / "retry.jpg"
    first_blob.write_bytes(b"same-phone-image")
    retry_blob.write_bytes(b"same-phone-image")
    meta = {"source_event_id": "pixel-7:image:20260621_120000_1", "captured_at": "2026-06-21T12:00:00+00:00"}
    first_id, first_reused = bridge.enqueue("image", "frame.jpg", first_blob, meta)
    second_id, second_reused = bridge.enqueue("image", "frame.jpg", retry_blob, meta)
    assert first_reused is False
    assert second_reused is True
    assert first_id == second_id

    with bridge.db() as con:
        rows = [dict(r) for r in con.execute("SELECT id,source_event_id,sha256 FROM incoming_items WHERE kind='image'")]
        row = con.execute("SELECT * FROM incoming_items WHERE id=?", (first_id,)).fetchone()
    assert len(rows) == 1
    assert rows[0]["source_event_id"] == meta["source_event_id"]
    target, parsed_meta = bridge.target_for_row(row)
    assert target and parsed_meta
    sidecar = bridge.normalized_sidecar("image", target, row, parsed_meta)
    assert sidecar["source_event_id"] == meta["source_event_id"]
    assert "phone_" in target.name
