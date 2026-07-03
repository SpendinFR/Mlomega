from __future__ import annotations

import os
from pathlib import Path

import pytest


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")
    monkeypatch.delenv("MLOMEGA_ALLOW_LEGACY_IMPLICIT_OWNER", raising=False)
    monkeypatch.delenv("MLOMEGA_V18_DECOMPOSED_LIVE", raising=False)
    return root


def _candidate_tick() -> dict:
    return {
        "shared_tick_id": "shared-analysis-1",
        "analysis": {
            "output": {
                "interventions": [
                    {
                        "candidate_id": "candidate-1",
                        "message": "Prends une minute pour vérifier le dossier.",
                        "recommended_timing": "now",
                        "cooldown_key": "check-dossier",
                        "urgency": 0.8,
                    }
                ]
            }
        },
    }


def test_legacy_daemon_transcript_enqueues_one_h1_delivery(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite import brainlive_daemon_v15_3 as daemon
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.brainlive_v15 import start_live_session

    session_id = start_live_session(person_id="me", title="daemon-dedupe-test")["live_session_id"]
    tick = _candidate_tick()
    monkeypatch.setattr(daemon, "live_cycle_all_horizons", lambda *_a, **_kw: {"H0": tick, "H1": tick, "H2": tick})
    result = daemon._consume_signal(
        session_id,
        {"signal_type": "transcript", "payload_json": '{"items":[{"text":"bonjour"}]}'},
        use_llm=False,
    )
    assert result["cycles"][0]["delivery_owner_horizon"] == "H1"
    assert len(result["cycles"][0]["delivery_ids"]) == 1
    # Defensive compatibility: even a legacy caller that retries every horizon
    # gets the winner, never additional queue rows.
    ids = [daemon.enqueue_interventions_from_tick(session_id, tick, delivery_owner_horizon="H1") for _ in range(3)]
    assert all(batch == ids[0] for batch in ids)
    with connect() as con:
        queue = con.execute("SELECT delivery_id,horizon FROM brainlive_intervention_delivery_queue").fetchall()
        dedupes = con.execute("SELECT dedupe_key,owner_horizon FROM brainlive_intervention_delivery_dedupes").fetchall()
    assert len(queue) == 1
    assert queue[0]["horizon"] == "H1"
    assert len(dedupes) == 1
    assert dedupes[0]["owner_horizon"] == "H1"


def test_legacy_owner_defaults_fail_closed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_daemon_v15_3 import configure_daemon
    from mlomega_audio_elite.brainlive_realtime_v15_2 import configure_runtime_profile
    from mlomega_audio_elite.governance_v18 import ScopeError

    with pytest.raises(ScopeError):
        configure_daemon()
    with pytest.raises(ScopeError):
        configure_runtime_profile()
    assert configure_daemon(person_id="me")["person_id"] == "me"


def test_v14_forecasts_are_selected_only_via_bounded_v18_lifecycle(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.pattern_mirror_v14 import ensure_v14_schema
    from mlomega_audio_elite.utils import now_iso
    from mlomega_audio_elite.v18_legacy_forecasts import (
        active_legacy_forecasts,
        reconcile_legacy_forecasts,
        record_legacy_forecast_outcome,
    )

    ensure_v14_schema()
    now = now_iso()
    with connect() as con:
        con.execute(
            """INSERT INTO v14_trajectory_forecasts(
                   forecast_id,card_id,person_id,current_situation,probable_path,probability,confidence,
                   risk_level,opportunity_level,time_horizon,early_warning_signals_json,escape_options_json,
                   evidence_json,status,created_at,updated_at
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("known", None, "me", "now", "later", 0.6, 0.7, "medium", "medium", "H1", "[]", "[]", "[]", "open", now, now),
        )
        con.execute(
            """INSERT INTO v14_trajectory_forecasts(
                   forecast_id,card_id,person_id,current_situation,probable_path,probability,confidence,
                   risk_level,opportunity_level,time_horizon,early_warning_signals_json,escape_options_json,
                   evidence_json,status,created_at,updated_at
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("unknown", None, "me", "now", "never-bound", 0.6, 0.7, "medium", "medium", "mystery_horizon", "[]", "[]", "[]", "open", now, now),
        )
        reconcile_legacy_forecasts(person_id="me", con=con)
        con.commit()
        active = active_legacy_forecasts(con, person_id="me", source_table="v14_trajectory_forecasts", limit=10)
    assert [row["forecast_id"] for row in active] == ["known"]
    closed = record_legacy_forecast_outcome(
        source_table="v14_trajectory_forecasts", source_id="known", person_id="me", correct=True, evidence={"observed": True}
    )
    assert closed["lifecycle_state"] == "evaluated_correct"
    with connect() as con:
        assert active_legacy_forecasts(con, person_id="me", source_table="v14_trajectory_forecasts", limit=10) == []
        state = con.execute("SELECT lifecycle_state FROM v18_legacy_forecast_lifecycle WHERE source_id='unknown'").fetchone()["lifecycle_state"]
    assert state == "indeterminate"


def test_decomposed_live_call_has_three_contracts_and_provenance(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_v15 as brainlive
    from mlomega_audio_elite.db import connect

    contexts = {
        "active_context_id": "ctx-1",
        "context": {
            "session": {"person_id": "me"},
            "active_people": [],
            "context_manifest": {"manifest_id": "manifest-1", "scope": {"person_id": "me", "live_session_id": "session", "as_of": "2026-06-21T10:00:00+00:00"}, "items": [{"source_table": "brainlive_turn_buffer", "source_id": "turn-1", "occurred_at": "2026-06-21T09:59:59+00:00", "retrievable": True, "truncated": False}]},
            "context_incomplete": False,
            "retrieval_policy": "source refs only",
        },
    }
    monkeypatch.setattr(brainlive, "build_active_context", lambda *_a, **_kw: contexts)

    class FakeClient:
        def __init__(self):
            self.n = 0

        def require_json(self, *_args, **_kwargs):
            self.n += 1
            if self.n == 1:
                return {
                    "world_state": {"active_mode": "work", "probable_activity": [], "confidence": 0.2, "evidence": [{"source_table": "brainlive_turn_buffer", "source_id": "turn-1"}], "counter_evidence": []},
                    "events": [], "need_predictions": [], "affordances": [],
                }
            if self.n == 2:
                return {"forecasts": [], "life_hypotheses": [], "watch_next": []}
            return {"interventions": [], "notes_for_brain2": []}

    monkeypatch.setattr(brainlive, "OllamaJsonClient", FakeClient)
    session = brainlive.start_live_session(person_id="me", title="decomposed-test")
    # V18.3 proves a cited manifest row still exists in the same session/scope.
    # The mock context must therefore model an actual persisted evidence row.
    contexts["context"]["context_manifest"]["scope"]["live_session_id"] = session["live_session_id"]
    with connect() as con:
        con.execute(
            """INSERT INTO brainlive_turn_buffer(
                 live_turn_id,live_session_id,conversation_id,timestamp_start,timestamp_end,
                 speaker_label,speaker_person_id,speaker_confidence,text_partial,text_final,
                 asr_confidence,is_final,metadata_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("turn-1", session["live_session_id"], None, "2026-06-21T09:59:59+00:00", None,
             "me", "me", 1.0, None, "preuve de test", 1.0, 1, "{}", "2026-06-21T09:59:59+00:00"),
        )
        con.commit()
    result = brainlive.run_brainlive(session["live_session_id"], use_llm=True)
    assert result["status"] == "ok"
    assert result["execution_mode"] == "decomposed_v18"
    with connect() as con:
        rows = con.execute("SELECT stage_name,status,input_manifest_id FROM brainlive_reasoning_stages_v18 ORDER BY stage_name").fetchall()
    assert [(r["stage_name"], r["status"], r["input_manifest_id"]) for r in rows] == [
        ("forecast", "ok", "manifest-1"),
        ("intervention", "ok", None),
        ("observation", "ok", "manifest-1"),
    ]


def test_release_audit_rejects_owner_escape_hatch(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MLOMEGA_ALLOW_LEGACY_IMPLICIT_OWNER", "true")
    from mlomega_audio_elite.v18_release_audit import audit_v18_release

    report = audit_v18_release(persist=False)
    assert report["status"] == "fail"
    assert "legacy_default_owner_escape_hatch_enabled" in {issue["code"] for issue in report["issues"]}


def test_v18_guide_and_bridge_contract_document_retention_gate():
    root = Path(__file__).resolve().parents[1]
    guide = (root / "GUIDE_INSTALL_MLOMEGA_V18_8_RUNTIME.md").read_text(encoding="utf-8")
    bridge = (root / "docs" / "V18_8_PHONE_BRIDGE_CONTRACT.md").read_text(encoding="utf-8")
    assert "v18-poststop-cleanup-check RUN_ID --person-id me" in guide
    assert "v14-interventions --person-id me" in guide
    assert "brainlive-delivery-queue LIVE_SESSION_ID --status queued" in guide
    assert "eligible=true" in bridge
