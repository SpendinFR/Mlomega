from __future__ import annotations

import pytest

from mlomega_audio_elite.db import connect, write_transaction
from mlomega_audio_elite.utils import now_iso


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")


def test_idempotent_pipeline_run_is_owner_scoped_and_resumable(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import Scope, begin_or_resume_run

    scope = Scope(person_id="alice", live_session_id="s1", mode="post_stop")
    first, resumed = begin_or_resume_run(
        pipeline_name="post_stop", scope=scope, input_manifest={"day": "2026-06-01"}, idempotency_key="day:s1"
    )
    second, resumed2 = begin_or_resume_run(
        pipeline_name="post_stop", scope=scope, input_manifest={"day": "2026-06-01"}, idempotency_key="day:s1"
    )
    assert first == second
    assert resumed is False and resumed2 is True

    # A source-run identity belongs to an owner scope, never to the entire DB.
    other, other_resumed = begin_or_resume_run(
        pipeline_name="post_stop",
        scope=Scope(person_id="bob", live_session_id="s1", mode="post_stop"),
        input_manifest={"day": "2026-06-01"},
        idempotency_key="day:s1",
    )
    assert other != first and other_resumed is False
    with connect() as con:
        row = con.execute("SELECT resume_count FROM v18_pipeline_runs WHERE run_id=?", (first,)).fetchone()
    assert row["resume_count"] == 1


def test_stale_stage_recovery_preserves_attempt_and_allows_next_attempt(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import (
        Scope,
        begin_or_resume_run,
        finish_stage,
        recover_stale_stages,
        start_stage,
    )

    run_id, _ = begin_or_resume_run(
        pipeline_name="post_stop", scope=Scope(person_id="me", mode="post_stop"), idempotency_key="recover-me"
    )
    with connect() as con, write_transaction(con):
        start_stage(con, run_id=run_id, stage_name="assembly")
        con.execute(
            "UPDATE v18_pipeline_stages SET started_at='2000-01-01T00:00:00+00:00' WHERE run_id=? AND stage_name='assembly'",
            (run_id,),
        )
        con.execute(
            "UPDATE v18_pipeline_stage_attempts SET started_at='2000-01-01T00:00:00+00:00' WHERE run_id=? AND stage_name='assembly' AND status='running'",
            (run_id,),
        )
    result = recover_stale_stages(run_id=run_id, stale_after_seconds=1)
    assert result["recovered"] == ["assembly"]

    with connect() as con, write_transaction(con):
        start_stage(con, run_id=run_id, stage_name="assembly")
        finish_stage(con, run_id=run_id, stage_name="assembly", result={"bundles": 1})
    with connect() as con:
        attempts = con.execute(
            "SELECT attempt_no,status FROM v18_pipeline_stage_attempts WHERE run_id=? ORDER BY attempt_no", (run_id,)
        ).fetchall()
    assert [(row["attempt_no"], row["status"]) for row in attempts] == [(1, "abandoned"), (2, "completed")]


def test_cleanup_gate_requires_completed_stages_and_retained_output_manifest(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import (
        Scope,
        StageGateError,
        assert_cleanup_eligible,
        begin_or_resume_run,
        finish_stage,
        record_output_manifest,
        start_stage,
        update_run,
    )

    run_id, _ = begin_or_resume_run(
        pipeline_name="post_stop", scope=Scope(person_id="me", mode="post_stop"), idempotency_key="cleanup-me"
    )
    with connect() as con, write_transaction(con):
        for name in ("assembly", "brain2"):
            start_stage(con, run_id=run_id, stage_name=name, required=True)
            finish_stage(con, run_id=run_id, stage_name=name, result={"status": "ok"})
    update_run(run_id, status="completed")
    record_output_manifest(run_id=run_id, person_id="me", expected=["conv-1"], observed=[], reason="simulated failure")
    with pytest.raises(StageGateError, match="manifest"):
        assert_cleanup_eligible(run_id=run_id, person_id="me", required_stages=["assembly", "brain2"])

    record_output_manifest(run_id=run_id, person_id="me", expected=["conv-1"], observed=["conv-1"])
    result = assert_cleanup_eligible(run_id=run_id, person_id="me", required_stages=["assembly", "brain2"])
    assert result["eligible"] is True


def _seed_conversation(con, *, conversation_id: str, people: list[str], duplicate_idx: bool = False):
    con.execute(
        """INSERT INTO conversations(conversation_id,title,started_at,ended_at,topic,channel,
               participants_json,speaker_map_json,relationship_context_json,source_asset_id,raw_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (conversation_id, conversation_id, "2026-06-01T10:00:00+00:00", None, None, "test", "[]", "{}", "{}", None, "{}", now_iso()),
    )
    for idx, person_id in enumerate(people):
        con.execute(
            """INSERT INTO turns(turn_id,conversation_id,idx,speaker_label,person_id,start_s,end_s,text,previous_turn_id,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f"{conversation_id}-{person_id}-{idx}", conversation_id, 0 if duplicate_idx else idx, person_id, person_id, float(idx), float(idx + 1), "x", None, "{}"),
        )


def test_legacy_migration_registers_only_unambiguous_user_scope_and_quarantines_structural_findings(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.v18_migration import run_legacy_migration
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.execute(
            "INSERT INTO speaker_profiles(person_id,display_name,is_user,aliases_json,notes,created_at) VALUES(?,?,?,?,?,?)",
            ("alice", "Alice", 1, "[]", None, now_iso()),
        )
        con.execute(
            "INSERT INTO speaker_profiles(person_id,display_name,is_user,aliases_json,notes,created_at) VALUES(?,?,?,?,?,?)",
            ("bob", "Bob", 1, "[]", None, now_iso()),
        )
        _seed_conversation(con, conversation_id="safe", people=["alice", "alice"], duplicate_idx=True)
        _seed_conversation(con, conversation_id="ambiguous", people=["alice", "bob"])

    result = run_legacy_migration(apply=True)
    assert result["migrated_count"] == 1
    assert result["quarantined_count"] >= 2  # duplicate idx + ambiguous owner
    with connect() as con:
        safe = con.execute(
            "SELECT person_id,evidence_kind FROM v18_conversation_scopes WHERE conversation_id='safe'"
        ).fetchall()
        ambiguous = con.execute(
            "SELECT COUNT(*) AS n FROM v18_conversation_scopes WHERE conversation_id='ambiguous'"
        ).fetchone()
        categories = {
            row["category"]
            for row in con.execute("SELECT category FROM data_quarantine_v176").fetchall()
        }
    assert [(row["person_id"], row["evidence_kind"]) for row in safe] == [("alice", "migration")]
    assert ambiguous["n"] == 0
    assert {"legacy_conversation_owner_ambiguous", "legacy_duplicate_turn_index"} <= categories


def test_requested_owner_does_not_turn_a_cross_user_legacy_conversation_into_a_proof(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema
    from mlomega_audio_elite.v18_migration import run_legacy_migration

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        for user in ("alice", "bob"):
            con.execute(
                "INSERT INTO speaker_profiles(person_id,display_name,is_user,aliases_json,notes,created_at) VALUES(?,?,?,?,?,?)",
                (user, user, 1, "[]", None, now_iso()),
            )
        _seed_conversation(con, conversation_id="cross-user", people=["alice", "bob"])
    result = run_legacy_migration(requested_person_id="alice", apply=True)
    assert result["migrated_count"] == 0
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM v18_conversation_scopes WHERE conversation_id='cross-user'"
        ).fetchone()
    assert row["n"] == 0


def test_claim_work_quarantines_corrupt_lease_schedule_instead_of_crashing(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import Scope, claim_work

    scope = Scope(person_id="me", live_session_id="s1", mode="live")
    first = claim_work(work_type="inbox:audio", scope=scope, source_key_value="audio-sha")
    assert first is not None
    # Simulate a legacy/corrupt row.  The next worker must not crash or steal a
    # lease with unknown chronology; it must make the item visibly quarantined.
    with connect() as con, write_transaction(con):
        con.execute(
            "UPDATE v18_work_leases SET state='retryable_error', retry_after='not-an-iso-date', lease_token=NULL, lease_expires_at=NULL WHERE work_key=?",
            (first["work_key"],),
        )
    assert claim_work(work_type="inbox:audio", scope=scope, source_key_value="audio-sha") is None
    with connect() as con:
        lease = con.execute("SELECT state,error_text FROM v18_work_leases WHERE work_key=?", (first["work_key"],)).fetchone()
        quarantine = con.execute(
            "SELECT category FROM data_quarantine_v176 WHERE source_table='v18_work_leases' AND source_id=?",
            (first["work_key"],),
        ).fetchone()
    assert lease["state"] == "quarantined"
    assert "invalid lease" in lease["error_text"]
    assert quarantine["category"] == "work_lease_metadata_invalid"


def test_release_audit_blocks_completed_poststop_without_complete_manifest(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import Scope, begin_or_resume_run, update_run
    from mlomega_audio_elite.v18_release_audit import audit_v18_release

    run_id, _ = begin_or_resume_run(
        pipeline_name="brainlive_post_stop",
        scope=Scope(person_id="me", mode="post_stop"),
        idempotency_key="release-audit-post-stop",
    )
    update_run(run_id, status="completed")
    report = audit_v18_release(stale_after_seconds=600)
    assert report["status"] == "fail"
    assert any(issue["code"] == "post_stop_completed_without_retained_manifest" for issue in report["issues"])


def test_release_audit_detects_active_projection_after_tombstone(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import Scope, ensure_v18_schema
    from mlomega_audio_elite.v18_release_audit import audit_v18_release

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        # Direct insertion simulates a legacy writer that bypassed the V18
        # invalidation gateway; the audit must expose it before release.
        con.execute(
            "INSERT INTO v18_invalidations(invalidation_id,root_table,root_id,person_id,reason,status,affected_json,created_at) VALUES(?,?,?,?,?,'completed','[]',?)",
            ("inv", "source_table", "source-1", "me", "test", now_iso()),
        )
        con.execute(
            "INSERT INTO v18_source_tombstones(source_table,source_id,person_id,invalidation_id,reason,invalidated_at) VALUES(?,?,?,?,?,?)",
            ("source_table", "source-1", "me", "inv", "test", now_iso()),
        )
        con.execute(
            "INSERT INTO v18_source_projection_state(projection_id,projection_kind,source_table,source_id,person_id,active,reason,created_at,updated_at) VALUES(?,?,?,?,?,1,NULL,?,?)",
            ("proj", "life_hook", "source_table", "source-1", "me", now_iso(), now_iso()),
        )
    report = audit_v18_release(stale_after_seconds=600)
    codes = {issue["code"] for issue in report["issues"]}
    assert "tombstoned_source_still_projected" in codes


def test_poststop_resume_reuses_completed_brain2_stage_without_reexecuting_deep_stack(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 import run_brainlive_post_stop_deep_flow
    from mlomega_audio_elite.brainlive_v15 import start_live_session
    from mlomega_audio_elite.governance_v18 import Scope, begin_or_resume_run, finish_stage, start_stage
    from mlomega_audio_elite.utils import stable_id
    from mlomega_audio_elite.config import get_settings
    import mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 as poststop
    import mlomega_audio_elite.brain2_flow_v13_3 as brain2_flow

    session_id = start_live_session(person_id="me", title="resume test")["live_session_id"]
    day = "2026-06-10"
    cfg = get_settings()
    # Seed the exact V18.7 semantic manifest. service_run_id is deliberately
    # excluded: a new supervisor process must reuse the same day/session run.
    idempotency_key = stable_id("poststop_v18_session", "me", session_id, day)
    run_id, _ = begin_or_resume_run(
        pipeline_name="brainlive_post_stop",
        scope=Scope(person_id="me", live_session_id=session_id, mode="post_stop"),
        input_manifest={
            "release": poststop.VERSION,
            "day": day,
            "limits": {"per_table": 5000, "gap_minutes": 20},
            "deep_audio": {"enabled": True, "language": "fr", "max_seconds": float(cfg.deep_audio_bundle_max_seconds), "model": cfg.whisperx_model, "device": cfg.whisperx_device, "compute": cfg.whisperx_compute_type},
            "deep_vision": {"enabled": False, "model": None},
            "brain2": {"enabled": True, "use_llm": True, "model": cfg.ollama_model},
        },
        idempotency_key=idempotency_key,
    )
    with connect() as con, write_transaction(con):
        start_stage(con, run_id=run_id, stage_name="assembly", required=True)
        finish_stage(con, run_id=run_id, stage_name="assembly", result={"status": "ok", "bundles": 1, "raw_rows": 1})
        start_stage(con, run_id=run_id, stage_name="brain2", required=True)
        finish_stage(
            con,
            run_id=run_id,
            stage_name="brain2",
            result={"processed": [{"conversation_id": "conv-retained", "bundle_id": "bundle-1", "status": "ok"}]},
        )

    monkeypatch.setattr(
        poststop,
        "_exported_bundle_conversations",
        lambda *args, **kwargs: [{"conversation_id": "conv-retained", "bundle_id": "bundle-1"}],
    )

    def _must_not_run(*args, **kwargs):
        raise AssertionError("completed Brain2 stage was incorrectly re-executed")

    monkeypatch.setattr(brain2_flow, "run_brain2_deep_stack_for_conversation", _must_not_run)
    result = run_brainlive_post_stop_deep_flow(
        person_id="me",
        live_session_id=session_id,
        package_date=day,
        run_deep_vision=False,
        run_silent_life=False,
        run_brain2=True,
        run_v15=True,
        use_llm=True,
    )
    assert result["status"] == "completed"
    assert result["brain2_processed"] == [{"conversation_id": "conv-retained", "bundle_id": "bundle-1", "status": "ok"}]
    assert result["v17_longitudinal"]["status"] == "deferred_session_scope"
    assert result["v15"]["status"] == "deferred_session_scope"
    with connect() as con:
        attempts = con.execute(
            "SELECT COUNT(*) AS n FROM v18_pipeline_stage_attempts WHERE run_id=? AND stage_name='brain2'", (run_id,)
        ).fetchone()
    assert attempts["n"] == 1


def test_idempotency_key_refuses_resume_with_different_manifest(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import Scope, StageGateError, begin_or_resume_run

    scope = Scope(person_id="me", live_session_id="same-session", mode="post_stop")
    begin_or_resume_run(
        pipeline_name="post_stop", scope=scope, input_manifest={"use_llm": False}, idempotency_key="same-logical-key"
    )
    with pytest.raises(StageGateError, match="different input/configuration manifest"):
        begin_or_resume_run(
            pipeline_name="post_stop", scope=scope, input_manifest={"use_llm": True}, idempotency_key="same-logical-key"
        )
