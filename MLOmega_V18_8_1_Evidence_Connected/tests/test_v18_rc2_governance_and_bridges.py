from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from mlomega_audio_elite.db import connect, write_transaction
from mlomega_audio_elite.governance_v18 import (
    ContextItem,
    Scope,
    StageGateError,
    begin_run,
    build_context_manifest,
    claim_work,
    finish_stage,
    finish_work,
    invalidate_descendants,
    link_artifact,
    projection_is_active,
    record_artifact_version,
    register_conversation_scope,
    set_projection_active,
    start_stage,
    update_run,
)
from mlomega_audio_elite.utils import json_dumps, now_iso


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")


def test_work_leases_are_owner_scoped_and_retryable(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    source = "raw-device-source-key"
    first = claim_work(work_type="inbox:audio", scope=Scope(person_id="alice", mode="live"), source_key_value=source)
    assert first is not None
    assert claim_work(work_type="inbox:audio", scope=Scope(person_id="alice", mode="live"), source_key_value=source) is None

    # Same phone/source occurrence for another memory owner must not be swallowed
    # by Alice's completed/retry state.
    second_owner = claim_work(work_type="inbox:audio", scope=Scope(person_id="bob", mode="live"), source_key_value=source)
    assert second_owner is not None
    assert second_owner["work_key"] != first["work_key"]

    finish_work(work_key=first["work_key"], lease_token=first["lease_token"], status="retryable_error", result={"why": "temporary"}, retry_delay_seconds=60)
    with connect() as con:
        rows = con.execute("SELECT person_id,source_key,state FROM v18_work_leases ORDER BY person_id").fetchall()
    assert [(r["person_id"], r["state"]) for r in rows] == [("alice", "retryable_error"), ("bob", "leased")]
    assert rows[0]["source_key"] != source


def test_stage_attempt_history_and_terminal_run_guard(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    run_id = begin_run(pipeline_name="post_stop", scope=Scope(person_id="me", mode="post_stop"))
    with connect() as con, write_transaction(con):
        start_stage(con, run_id=run_id, stage_name="assembly", input_payload={"bundle": 1})
        finish_stage(con, run_id=run_id, stage_name="assembly", status="failed", result={"error": "transient"}, error_text="transient")
        start_stage(con, run_id=run_id, stage_name="assembly", input_payload={"bundle": 1})
        finish_stage(con, run_id=run_id, stage_name="assembly", status="completed", result={"bundles": 1})

    with connect() as con:
        attempts = con.execute(
            "SELECT attempt_no,status FROM v18_pipeline_stage_attempts WHERE run_id=? AND stage_name='assembly' ORDER BY attempt_no",
            (run_id,),
        ).fetchall()
    assert [(r["attempt_no"], r["status"]) for r in attempts] == [(1, "failed"), (2, "completed")]

    update_run(run_id, status="completed")
    with pytest.raises(StageGateError):
        update_run(run_id, status="running")
    with pytest.raises(StageGateError):
        update_run("missing-run", status="completed")


def test_artifact_invalidation_is_owner_scoped_and_revokes_all_projections(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    alice = Scope(person_id="alice", mode="maintenance")
    bob = Scope(person_id="bob", mode="maintenance")

    # Same logical identity in two profiles is legal and must not supersede the
    # other profile.
    a_root = record_artifact_version(
        artifact_table="life_model", artifact_id="root", identity_key="routine:morning", scope=alice, source_payload={"v": 1}
    )
    b_root = record_artifact_version(
        artifact_table="life_model", artifact_id="root_bob", identity_key="routine:morning", scope=bob, source_payload={"v": 1}
    )
    assert a_root["created"] and b_root["created"]

    record_artifact_version(
        artifact_table="live_hook", artifact_id="child", identity_key="hook:morning", scope=alice, source_payload={"v": 1}
    )
    link_artifact(child_table="live_hook", child_id="child", parent_table="life_model", parent_id="root", scope=alice, relation_type="derived_from")
    set_projection_active(projection_kind="life_model", source_table="life_model", source_id="root", person_id="alice", active=True)
    set_projection_active(projection_kind="watch", source_table="live_hook", source_id="child", person_id="alice", active=True)

    result = invalidate_descendants(root_table="life_model", root_id="root", scope=alice, reason="contradicted")
    assert {tuple((x["table"], x["id"])) for x in result["affected"]} == {("life_model", "root"), ("live_hook", "child")}
    with connect() as con:
        assert not projection_is_active(con, projection_kind="life_model", source_table="life_model", source_id="root", person_id="alice")
        assert not projection_is_active(con, projection_kind="watch", source_table="live_hook", source_id="child", person_id="alice")
        assert projection_is_active(con, projection_kind="life_model", source_table="life_model", source_id="root_bob", person_id="bob")

    # Replaying exactly the old payload cannot revive contradicted material.
    same = record_artifact_version(
        artifact_table="life_model", artifact_id="root", identity_key="routine:morning", scope=alice, source_payload={"v": 1}
    )
    assert same["version"] == 2
    with connect() as con:
        assert not projection_is_active(con, projection_kind="life_model", source_table="life_model", source_id="root", person_id="alice")

    # A genuinely changed source version may be rebuilt, while the child still
    # needs its own rebuild/revalidation.
    changed = record_artifact_version(
        artifact_table="life_model", artifact_id="root", identity_key="routine:morning", scope=alice, source_payload={"v": 2}
    )
    assert changed["version"] == 3
    # A changed artifact clears the source tombstone, but an explicit policy
    # revocation still requires a deliberate revalidation step.
    with connect() as con:
        assert not projection_is_active(con, projection_kind="life_model", source_table="life_model", source_id="root", person_id="alice")
        assert not projection_is_active(con, projection_kind="watch", source_table="live_hook", source_id="child", person_id="alice")
    set_projection_active(projection_kind="life_model", source_table="life_model", source_id="root", person_id="alice", active=True, reason="revalidated")
    with connect() as con:
        assert projection_is_active(con, projection_kind="life_model", source_table="life_model", source_id="root", person_id="alice")


def test_context_manifest_exposes_truncation_and_future_exclusion(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    scope = Scope(person_id="me", as_of="2026-01-01T10:00:00+00:00", mode="replay")
    manifest = build_context_manifest(
        scope=scope,
        purpose="predictive_retrieval",
        max_chars=4,
        max_item_chars=4,
        items=[
            ContextItem("turns", "past", "me", "2026-01-01T09:59:00+00:00", "abcdef", importance=1.0),
            ContextItem("turns", "future", "me", "2026-01-01T10:01:00+00:00", "future", importance=1.0),
        ],
    )
    assert [x["source_id"] for x in manifest["items"]] == ["past"]
    assert manifest["items"][0]["truncated"] is True
    assert manifest["incomplete"] is True
    assert manifest["excluded_future_refs"][0]["source_id"] == "future"


def test_isolated_replay_keeps_source_time_and_never_writes_live_tables(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_longitudinal_v15_1 import replay_offline
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.execute(
            "INSERT INTO conversations(conversation_id,title,started_at,ended_at,topic,channel,participants_json,speaker_map_json,relationship_context_json,source_asset_id,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("conv", "c", "2026-01-01T10:00:00+00:00", None, None, "audio", "[]", "{}", "{}", None, "{}", now_iso()),
        )
        for idx, (turn_id, offset, text) in enumerate((("t1", 0, "first"), ("t2", 10, "second"), ("t3", 20, "third"))):
            con.execute(
                "INSERT INTO turns(turn_id,conversation_id,idx,speaker_label,person_id,start_s,end_s,text,previous_turn_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (turn_id, "conv", idx, "me", "me", float(offset), float(offset + 1), text, None, "{}"),
            )
    register_conversation_scope(conversation_id="conv", person_id="me", evidence_kind="turn_owner")
    result = replay_offline(
        person_id="me", conversation_id="conv", start_time="2026-01-01T10:00:09+00:00", end_time="2026-01-01T10:00:11+00:00"
    )
    assert result["turn_count"] == 1
    assert result["steps"][0]["turn_ids"] == ["t2"]
    assert result["steps"][0]["source_times"] == ["2026-01-01T10:00:10.000+00:00"]
    with connect() as con:
        assert con.execute("SELECT COUNT(*) AS n FROM brainlive_sessions").fetchone()["n"] == 0
        assert con.execute("SELECT COUNT(*) AS n FROM brainlive_turn_buffer").fetchone()["n"] == 0
        assert con.execute("SELECT COUNT(*) AS n FROM v18_replay_runs").fetchone()["n"] == 1


def test_v17_period_selection_never_assigns_other_owner_conversations(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import _conversation_ids_for_period, ensure_longitudinal_case_schema

    ensure_longitudinal_case_schema()
    with connect() as con, write_transaction(con):
        for cid, owner in (("alice_conv", "alice"), ("bob_conv", "bob")):
            con.execute(
                "INSERT INTO conversations(conversation_id,title,started_at,ended_at,topic,channel,participants_json,speaker_map_json,relationship_context_json,source_asset_id,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, cid, "2026-01-01T10:00:00+00:00", None, None, "audio", "[]", "{}", "{}", None, "{}", now_iso()),
            )
            con.execute(
                "INSERT INTO turns(turn_id,conversation_id,idx,speaker_label,person_id,start_s,end_s,text,previous_turn_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"{cid}_t", cid, 0, owner, owner, 0.0, 1.0, "owned", None, "{}"),
            )
    register_conversation_scope(conversation_id="alice_conv", person_id="alice", evidence_kind="turn_owner")
    register_conversation_scope(conversation_id="bob_conv", person_id="bob", evidence_kind="turn_owner")
    with connect() as con:
        ids = _conversation_ids_for_period(con, person_id="alice", period_start="2026-01-01T00:00:00+00:00", period_end="2026-01-02T00:00:00+00:00")
    assert ids == ["alice_conv"]


def test_deep_vision_explicit_revocation_blocks_brain2_addendum(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import ensure_event_assembler_schema
    from mlomega_audio_elite.brainlive_offline_deep_vision_v16_1 import append_deep_vision_context_turns_to_brain2, ensure_deep_vision_schema

    ensure_event_assembler_schema()
    ensure_deep_vision_schema()
    stamp = "2026-01-01T12:00:00+00:00"
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO brainlive_event_bundles_v1514(bundle_id,person_id,package_date,live_session_id,start_time,end_time,bundle_kind,title,brain2_conversation_id,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("b1", "me", "2026-01-01", "s1", stamp, stamp, "mixed", "bundle", "conv1", "assembled", now_iso(), now_iso()),
        )
        con.execute(
            "INSERT INTO brainlive_brain2_event_exports_v1514(export_id,person_id,bundle_id,conversation_id,turn_ids_json,export_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("e1", "me", "b1", "conv1", "[]", "exported", now_iso(), now_iso()),
        )
        con.execute(
            """INSERT INTO brainlive_deep_vision_observations_v161(deep_observation_id,run_id,person_id,package_date,bundle_id,live_session_id,conversation_id,frame_id,image_path,frame_time,sample_index,model,status,scene_summary_detailed,observed_activity,activity_confidence,objects_json,affordances_json,visible_text_json,people_presence_json,screens_or_devices_json,posture_motion_json,work_or_rest_signal_json,smoking_pause_signal_json,exact_visual_evidence_json,uncertainty_json,qwen_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("d1", "r1", "me", "2026-01-01", "b1", "s1", "conv1", "f1", "/tmp/image.jpg", stamp, 0, "mock", "ok", "desk", "working", 0.8, "[]", "[]", "[]", "{}", "[]", "{}", "{}", "{}", "[]", "[]", "{}", now_iso(), now_iso()),
        )
    set_projection_active(projection_kind="deep_vision", source_table="brainlive_deep_vision_observations_v161", source_id="d1", person_id="me", active=False, reason="revoked")
    blocked = append_deep_vision_context_turns_to_brain2("me", package_date="2026-01-01")
    assert blocked["context_addenda_created"] == 0
    with connect() as con:
        assert con.execute("SELECT COUNT(*) AS n FROM brain2_context_addenda_v18").fetchone()["n"] == 0

    set_projection_active(projection_kind="deep_vision", source_table="brainlive_deep_vision_observations_v161", source_id="d1", person_id="me", active=True, reason="revalidated")
    allowed = append_deep_vision_context_turns_to_brain2("me", package_date="2026-01-01")
    assert allowed["context_addenda_created"] == 1


def test_sync_job_claim_is_atomic_and_terminal_jobs_do_not_relaunch(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.sync_jobs import SyncNotRunnable, begin_sync_job, complete_sync_job, ensure_sync_job
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        job_id = ensure_sync_job(
            con,
            backend="vector:qdrant",
            operation="upsert",
            target_table="memory_cards",
            target_id="m1",
            conversation_id=None,
            payload={"source_version": "1", "person_id": "me"},
        )
    token = begin_sync_job(job_id)
    with pytest.raises(SyncNotRunnable):
        begin_sync_job(job_id)
    complete_sync_job(job_id, result={"ok": True}, token=token)
    with pytest.raises(SyncNotRunnable):
        begin_sync_job(job_id)


def test_life_model_general_stratum_requires_independent_days():
    from mlomega_audio_elite.governance_v18 import ScopeError
    from mlomega_audio_elite.v18_life_model import validate_stratum_evidence

    same_day = [
        {"source_table": "turns", "source_id": f"t{i}", "occurred_at": "2026-01-01T10:00:00+00:00"}
        for i in range(3)
    ]
    with pytest.raises(ScopeError):
        validate_stratum_evidence(refs=same_day, stratum="general")

    independent = [
        {"source_table": "turns", "source_id": f"t{i}", "occurred_at": f"2026-01-0{i+1}T10:00:00+00:00"}
        for i in range(3)
    ]
    validate_stratum_evidence(refs=independent, stratum="general")
    # Recent claims may legitimately be based on a single observation, but the
    # caller still needs the separate source-reference validator.
    validate_stratum_evidence(refs=same_day[:1], stratum="recent")


def test_replay_legacy_owner_migration_uses_valid_writer_boundary(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_longitudinal_v15_1 import replay_offline
    from mlomega_audio_elite.governance_v18 import conversation_in_scope, ensure_v18_schema

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.execute(
            "INSERT INTO conversations(conversation_id,title,started_at,ended_at,topic,channel,participants_json,speaker_map_json,relationship_context_json,source_asset_id,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy_conv", "legacy", "2026-01-02T10:00:00+00:00", None, None, "audio", "[]", "{}", "{}", None, "{}", now_iso()),
        )
        con.execute(
            "INSERT INTO turns(turn_id,conversation_id,idx,speaker_label,person_id,start_s,end_s,text,previous_turn_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("legacy_t", "legacy_conv", 0, "me", "me", 0.0, 1.0, "owned", None, "{}"),
        )
    # No v18_conversation_scopes row exists: replay must create it after its
    # read transaction, not call a nested writer with an invalid signature.
    result = replay_offline(person_id="me", conversation_id="legacy_conv")
    assert result["status"] == "completed"
    with connect() as con:
        assert conversation_in_scope(con, conversation_id="legacy_conv", person_id="me", allow_legacy_turn_proof=False)


def test_longitudinal_legacy_scope_migration_is_safe_inside_writer_transaction(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import _conversation_ids_for_period, ensure_longitudinal_case_schema
    from mlomega_audio_elite.governance_v18 import conversation_in_scope

    ensure_longitudinal_case_schema()
    with connect() as con, write_transaction(con):
        con.execute(
            "INSERT INTO conversations(conversation_id,title,started_at,ended_at,topic,channel,participants_json,speaker_map_json,relationship_context_json,source_asset_id,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy_long", "legacy", "2026-01-03T10:00:00+00:00", None, None, "audio", "[]", "{}", "{}", None, "{}", now_iso()),
        )
        con.execute(
            "INSERT INTO turns(turn_id,conversation_id,idx,speaker_label,person_id,start_s,end_s,text,previous_turn_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("legacy_long_t", "legacy_long", 0, "alice", "alice", 0.0, 1.0, "owned", None, "{}"),
        )
        ids = _conversation_ids_for_period(
            con, person_id="alice", period_start="2026-01-03T00:00:00+00:00", period_end="2026-01-04T00:00:00+00:00"
        )
        assert ids == ["legacy_long"]
        assert conversation_in_scope(con, conversation_id="legacy_long", person_id="alice", allow_legacy_turn_proof=False)


def test_v13_episode_bundle_is_local_with_explicit_boundary_window(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brain2_strict_v13_2 import _episode_bundle, ensure_strict_v13_schema

    ensure_strict_v13_schema()
    with connect() as con, write_transaction(con):
        con.execute(
            "INSERT INTO conversations(conversation_id,title,started_at,ended_at,topic,channel,participants_json,speaker_map_json,relationship_context_json,source_asset_id,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("v13conv", "v13", "2026-01-04T10:00:00+00:00", None, None, "audio", "[]", "{}", "{}", None, "{}", now_iso()),
        )
        for idx in range(10):
            con.execute(
                "INSERT INTO turns(turn_id,conversation_id,idx,speaker_label,person_id,start_s,end_s,text,previous_turn_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"t{idx}", "v13conv", idx, "me", "me", float(idx), float(idx + 1), f"turn {idx}", None, "{}"),
            )
        con.execute(
            """INSERT INTO episodes(episode_id,episode_type,source_conversation_id,start_turn_id,end_turn_id,situation_summary,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("ep_local", "conversation", "v13conv", "t3", "t5", "local", now_iso(), now_iso()),
        )
        bundle = _episode_bundle(con, "ep_local")
    ids = [row["turn_id"] for row in bundle["turns"]]
    assert ids == ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
    assert bundle["context_scope"]["local_only"] is True
    assert bundle["context_scope"]["turn_count"] == 7


def test_v17_predictive_similarity_excludes_future_cases(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import compute_global_case_similarities, ensure_longitudinal_case_schema

    ensure_longitudinal_case_schema()
    with connect() as con, write_transaction(con):
        for cid, at in (("past", "2026-01-05T09:00:00+00:00"), ("anchor", "2026-01-05T10:00:00+00:00"), ("future", "2026-01-05T11:00:00+00:00")):
            con.execute(
                """INSERT INTO brain2_observed_cases_v17(
                    observed_case_id,person_id,case_type,case_key,title,context_summary,
                    people_json,tags_json,comparable_vector_json,embedding_text,quality_score,confidence,
                    observed_at,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, "me", "work", "work|tags:focus", cid, "focus work", "[]", '["focus"]', '{"outcome_tokens":["future_only"]}', "focus work", .7, .7, at, "active", now_iso(), now_iso()),
            )
    # RC5 intentionally has no lexical/Jaccard fallback. In a unit environment
    # without the configured Qdrant + dense model stack, V17 must abstain rather
    # than pretend that the historical token overlap is a predictive edge. The
    # fully dense causal path is covered by RC5-specific mocked-backend tests.
    with connect() as con, write_transaction(con):
        con.execute("UPDATE brain2_observed_cases_v17 SET source_version='unit-v1'")
    result = compute_global_case_similarities(person_id="me", anchor_case_ids=["anchor"], min_score=0.0, top_k=10, mode="predictive")
    assert result["status"] == "abstained"
    assert result["edges_upserted"] == 0
    with connect() as con:
        edges = con.execute(
            "SELECT anchor_case_id,similar_case_id FROM brain2_case_similarity_edges_v17 WHERE person_id='me' AND status='active'"
        ).fetchall()
    assert edges == []


def test_v14_router_rejects_cross_owner_raw_candidates(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema
    from mlomega_audio_elite.brain2_router_v14_1 import _v18_candidate_owner_ok_v141

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        for cid, owner in (("a_conv", "alice"), ("b_conv", "bob")):
            con.execute(
                "INSERT INTO conversations(conversation_id,title,started_at,ended_at,topic,channel,participants_json,speaker_map_json,relationship_context_json,source_asset_id,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, cid, "2026-01-06T10:00:00+00:00", None, None, "audio", "[]", "{}", "{}", None, "{}", now_iso()),
            )
            con.execute(
                "INSERT INTO turns(turn_id,conversation_id,idx,speaker_label,person_id,start_s,end_s,text,previous_turn_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"{owner}_turn", cid, 0, owner, owner, 0.0, 1.0, "private", None, "{}"),
            )
    register_conversation_scope(conversation_id="a_conv", person_id="alice", evidence_kind="turn_owner")
    register_conversation_scope(conversation_id="b_conv", person_id="bob", evidence_kind="turn_owner")
    with connect() as con:
        assert _v18_candidate_owner_ok_v141(con, {"source_table": "turns", "source_id": "alice_turn"}, "alice")
        assert not _v18_candidate_owner_ok_v141(con, {"source_table": "turns", "source_id": "bob_turn"}, "alice")


def test_v14_vector_fusion_merges_same_source_once():
    from mlomega_audio_elite.brain2_router_v14_2 import _fusion_rows

    fused = _fusion_rows(
        [{"source_kind": "memory", "source_table": "memory_cards", "source_id": "m1", "score": 0.4, "payload_json": '{"text":"SQL evidence"}'}],
        [{"source_type": "memory_card", "source_id": "m1", "score": 0.9, "text": "vector evidence", "metadata": {"person_id": "me"}}],
    )
    assert len(fused) == 1
    assert fused[0]["source_table"] == "memory_cards"
    assert fused[0]["source_id"] == "m1"
    assert fused[0]["came_from_sql"] == 1
    assert fused[0]["came_from_vector"] == 1
    assert fused[0]["sql_score"] == 0.4
    assert fused[0]["vector_score"] == 0.9


def test_post_stop_failure_is_a_barrier_not_a_partial_learning_run(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 import run_brainlive_post_stop_deep_flow
    import mlomega_audio_elite.brainlive_event_assembler_v15_14 as assembler
    import mlomega_audio_elite.brainlive_offline_deep_vision_v16_1 as deep_vision

    calls = {"deep": 0}
    monkeypatch.setattr(assembler, "run_brainlive_event_assembly", lambda **_: {"status": "partial", "bundles": 0})
    monkeypatch.setattr(
        deep_vision,
        "run_offline_deep_vision_for_bundles",
        lambda **_: calls.__setitem__("deep", calls["deep"] + 1) or {"status": "ok"},
    )
    result = run_brainlive_post_stop_deep_flow(
        person_id="me", package_date="2026-01-07", run_deep_vision=True,
        run_silent_life=True, run_brain2=True, run_v15=True, use_llm=False,
    )
    assert result["status"] == "blocked"
    assert calls["deep"] == 0
    with connect() as con:
        stages = con.execute(
            "SELECT s.stage_name,s.status FROM v18_pipeline_stages s JOIN v18_pipeline_runs r ON r.run_id=s.run_id WHERE r.pipeline_name='brainlive_post_stop'"
        ).fetchall()
    assert [(row["stage_name"], row["status"]) for row in stages] == [("assembly", "blocked")]
