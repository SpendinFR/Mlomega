from __future__ import annotations

import json

import pytest

from mlomega_audio_elite.db import connect, write_transaction
from mlomega_audio_elite.utils import now_iso


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")


def test_event_envelope_conflict_is_quarantined_not_silently_deduped(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import EventTime, GovernanceError, Scope, register_event

    args = dict(
        scope=Scope(person_id="me", mode="live"),
        modality="audio",
        source_device="phone-A",
        source_event_id="evt-42",
        source_sha256="sha-42",
        time=EventTime(
            occurred_at="2026-02-01T10:00:00+00:00",
            captured_at="2026-02-01T10:00:00+00:00",
            received_at="2026-02-01T10:00:01+00:00",
        ),
        source_path="/capture/a.wav",
    )
    created = register_event(**args, payload={"transcript": "one"})
    assert created["created"] is True
    with pytest.raises(GovernanceError, match="conflict"):
        register_event(**args, payload={"transcript": "two"})
    with connect() as con:
        rows = con.execute(
            "SELECT category FROM data_quarantine_v176 WHERE category='event_envelope_conflict'"
        ).fetchall()
        events = con.execute("SELECT COUNT(*) AS n FROM event_envelopes_v176").fetchone()
    assert len(rows) == 1
    assert events["n"] == 1


def test_context_gateway_deduplicates_source_without_hiding_the_audit_ref(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import ContextItem, Scope, build_context_manifest

    manifest = build_context_manifest(
        scope=Scope(person_id="me", as_of="2026-02-01T12:00:00+00:00", mode="replay"),
        purpose="prediction",
        max_chars=500,
        items=[
            ContextItem("turns", "t-1", "me", "2026-02-01T11:00:00+00:00", "short SQL copy", importance=0.4),
            ContextItem("turns", "t-1", "me", "2026-02-01T11:00:00+00:00", "richer canonical copy", importance=0.9),
        ],
    )
    assert len(manifest["items"]) == 1
    assert manifest["items"][0]["text"] == "richer canonical copy"
    assert manifest["deduplicated_refs"] == [
        {
            "source_table": "turns",
            "source_id": "t-1",
            "version": None,
            "occurred_at": "2026-02-01T11:00:00+00:00",
            "reason": "duplicate_source_ref",
        }
    ]


def test_malformed_descriptor_is_durably_quarantined_but_a_repaired_sidecar_is_eligible(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_service_v15_5 import (
        _already_processed,
        _file_sha,
        _quarantine_unclaimable_input,
    )

    media = tmp_path / "broken.wav"
    media.write_bytes(b"not-a-real-wav-but-a-nonempty-capture")
    _quarantine_unclaimable_input(
        media, kind="audio", person_id="me", live_session_id="session", error=ValueError("missing timestamp")
    )
    with connect() as con:
        assert _already_processed(con, "session", media, _file_sha(media), person_id="me", kind="audio")
    (tmp_path / "broken.wav.json").write_text(
        json.dumps(
            {
                "timestamp_start": "2026-02-02T10:00:00+00:00",
                "source_device": "phone-A",
                "source_event_id": "repaired-1",
                "sha256": _file_sha(media),
            }
        ),
        encoding="utf-8",
    )
    with connect() as con:
        assert not _already_processed(con, "session", media, _file_sha(media), person_id="me", kind="audio")


def test_sync_backoff_uses_instants_not_lexical_offset_strings(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.v18_sync as v18_sync
    import mlomega_audio_elite.sync_jobs as sync_jobs
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema

    ensure_v18_schema()
    # 23:00Z equals 01:00+02. The old lexical comparison treated the latter
    # as later only because "2026" sorted after "2025".
    monkeypatch.setattr(v18_sync, "now_iso", lambda: "2025-12-31T23:00:00+00:00")
    with connect() as con, write_transaction(con):
        job_id = sync_jobs.ensure_sync_job(
            con,
            backend="vector:test",
            operation="upsert_incremental",
            target_table="memory_cards",
            target_id="m1",
            payload={"test": True},
        )
        con.execute(
            "UPDATE sync_jobs SET next_attempt_at=? WHERE job_id=?",
            ("2026-01-01T01:00:00+02:00", job_id),
        )
    token = sync_jobs.begin_sync_job(job_id)
    assert token
    sync_jobs.complete_sync_job(job_id, token=token, result={"ok": True})
    with connect() as con:
        assert con.execute("SELECT status FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()["status"] == "succeeded"


def test_invalid_sync_schedule_is_terminal_visible_not_silently_skipped(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.sync_jobs as sync_jobs
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        job_id = sync_jobs.ensure_sync_job(
            con,
            backend="vector:test",
            operation="upsert_incremental",
            target_table="memory_cards",
            target_id="m2",
            payload={"test": True},
        )
        con.execute("UPDATE sync_jobs SET next_attempt_at=? WHERE job_id=?", ("not-a-time", job_id))
    with pytest.raises(sync_jobs.SyncNotRunnable, match="invalid next_attempt_at"):
        sync_jobs.begin_sync_job(job_id)
    with connect() as con:
        row = con.execute("SELECT status,error_message FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
    assert row["status"] == "dead"
    assert "invalid next_attempt_at" in row["error_message"]


def test_external_projection_manifest_is_owner_scoped(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.v18_external import _mark
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema

    ensure_v18_schema()
    _mark("graphiti", "legacy-conversation-id", "alice", active=True, status="active", detail={"source_version": "a"})
    _mark("graphiti", "legacy-conversation-id", "bob", active=False, status="disabled", detail={"source_version": "b"})
    with connect() as con:
        rows = con.execute(
            "SELECT person_id,active,truth_status FROM v18_external_sync_manifest ORDER BY person_id"
        ).fetchall()
    assert [(r["person_id"], r["active"], r["truth_status"]) for r in rows] == [
        ("alice", 1, "active"),
        ("bob", 0, "disabled"),
    ]


def test_vector_points_are_owner_scoped(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.v18_sync import vector_point_id

    assert vector_point_id(person_id="alice", source_type="memory_card", source_id="same") != vector_point_id(
        person_id="bob", source_type="memory_card", source_id="same"
    )
    with pytest.raises(ValueError):
        vector_point_id(person_id="", source_type="memory_card", source_id="same")


def test_life_model_as_of_is_forwarded_to_legacy_runner_as_period_end(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brain2_life_model_updater_v15_13 as updater

    captured: dict[str, object] = {}

    def fake_runner(person_id, **kwargs):
        captured["person_id"] = person_id
        captured.update(kwargs)
        return {"status": "fake"}

    monkeypatch.setattr(updater, "_v17_run_brain2_life_model_update", fake_runner)
    result = updater.run_brain2_life_model_update(
        "me", period_start="2026-02-01T00:00:00+00:00", as_of="2026-02-03T00:00:00+00:00", use_llm=False
    )
    assert captured["period_end"] == "2026-02-03T00:00:00.000+00:00"
    assert result["as_of"] == "2026-02-03T00:00:00.000+00:00"
    assert result["effective_period_end"] == "2026-02-03T00:00:00.000+00:00"


def test_replay_rejects_bad_time_metadata_without_creating_a_replay_run(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_longitudinal_v15_1 import replay_offline
    from mlomega_audio_elite.governance_v18 import ScopeError, ensure_v18_schema, register_conversation_scope

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.execute(
            "INSERT INTO conversations(conversation_id,title,started_at,ended_at,topic,channel,participants_json,speaker_map_json,relationship_context_json,source_asset_id,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("bad-replay", "bad", "2026-02-04T10:00:00+00:00", None, None, "audio", "[]", "{}", "{}", None, "{}", now_iso()),
        )
        con.execute(
            "INSERT INTO turns(turn_id,conversation_id,idx,speaker_label,person_id,start_s,end_s,text,previous_turn_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("bad-turn", "bad-replay", 0, "me", "me", 0.0, 1.0, "bad", None, '{"occurred_at":"not-a-time"}'),
        )
    register_conversation_scope(conversation_id="bad-replay", person_id="me", evidence_kind="turn_owner")
    with pytest.raises(ScopeError, match="invalid occurred_at"):
        replay_offline(person_id="me", conversation_id="bad-replay")
    with connect() as con:
        assert con.execute("SELECT COUNT(*) AS n FROM v18_replay_runs").fetchone()["n"] == 0


def test_incomplete_context_cannot_call_llm_or_create_live_outputs(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_v15 as brainlive

    class ForbiddenClient:
        def require_json(self, *args, **kwargs):
            raise AssertionError("LLM must not run on an incomplete manifest")

    session = brainlive.start_live_session(person_id="me")
    monkeypatch.setattr(
        brainlive,
        "build_active_context",
        lambda *_args, **_kwargs: {
            "active_context_id": "incomplete-context",
            "context": {"session": {"person_id": "me"}, "context_incomplete": True},
        },
    )
    monkeypatch.setattr(brainlive, "OllamaJsonClient", ForbiddenClient)
    result = brainlive.run_brainlive(session["live_session_id"], use_llm=True)
    assert result["status"] == "context_incomplete"
    assert result["counts"]["forecasts"] == 0
    with connect() as con:
        assert con.execute("SELECT COUNT(*) AS n FROM brainlive_short_horizon_forecasts").fetchone()["n"] == 0


def test_human_action_allowlist_rejects_unknown_target_table(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.v18_interactions import _row_owner
    from mlomega_audio_elite.governance_v18 import ScopeError, ensure_v18_schema

    ensure_v18_schema()
    with connect() as con:
        with pytest.raises(ScopeError, match="not approved"):
            _row_owner(con, "some_untrusted_table", "x")


def _seed_conversation_scope(monkeypatch, tmp_path, *, conversation_id: str = "scoped-conv", person_id: str = "me"):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema, register_conversation_scope

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO conversations(
                   conversation_id,title,started_at,ended_at,topic,channel,
                   participants_json,speaker_map_json,relationship_context_json,
                   source_asset_id,raw_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                conversation_id,
                "scoped",
                "2026-02-05T10:00:00+00:00",
                None,
                "test",
                "transcript",
                "[]",
                "{}",
                "{}",
                None,
                "{}",
                now_iso(),
            ),
        )
    register_conversation_scope(
        conversation_id=conversation_id,
        person_id=person_id,
        evidence_kind="explicit_export",
        evidence={"test": True},
    )
    return conversation_id


def test_post_ingest_sync_jobs_are_owner_scoped_and_worker_can_run_external_gate(monkeypatch, tmp_path):
    conversation_id = _seed_conversation_scope(monkeypatch, tmp_path)
    import mlomega_audio_elite.sync_jobs as sync_jobs

    with connect() as con, write_transaction(con):
        job_ids = sync_jobs.schedule_post_ingest_sync(
            con, conversation_id=conversation_id, person_id="me"
        )
    # CORE_BRAINLIVE_V18_7 is intentionally graph-free: only the scoped
    # vector/Qdrant projection is scheduled. Graphiti and Mem0 must not leave
    # inert jobs that a legacy worker could wake later.
    assert len(job_ids) == 1
    with connect() as con:
        rows = con.execute(
            "SELECT backend,payload_json FROM sync_jobs WHERE job_id=?",
            (job_ids[0],),
        ).fetchall()
    payloads = [json.loads(row["payload_json"]) for row in rows]
    assert {payload["person_id"] for payload in payloads} == {"me"}
    assert {payload["conversation_id"] for payload in payloads} == {conversation_id}
    assert str(rows[0]["backend"]).startswith("vector:")

    # The unit environment deliberately has no embedding model. The worker
    # therefore records the missing dependency as one visible retryable job;
    # it must never enqueue Graphiti/Mem0 as a fallback.
    result = sync_jobs.run_pending_sync_jobs(limit=5)
    assert result[0]["status"] == "failed"
    with connect() as con:
        statuses = con.execute(
            "SELECT backend,status FROM sync_jobs WHERE conversation_id=? ORDER BY backend",
            (conversation_id,),
        ).fetchall()
    assert len(statuses) == 1
    assert str(statuses[0]["backend"]).startswith("vector:")
    assert statuses[0]["status"] == "failed"


def test_ownerless_legacy_sync_job_is_dead_lettered_not_executed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.sync_jobs as sync_jobs
    from mlomega_audio_elite.governance_v18 import ensure_v18_schema

    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        job_id = sync_jobs.ensure_sync_job(
            con,
            backend="vector:test",
            operation="upsert_incremental",
            target_table="all_memory",
            target_id="legacy-global",
            payload={"conversation_id": None, "reason": "legacy"},
        )
    result = sync_jobs.run_pending_sync_jobs(limit=5)
    assert any(item.get("job_id") == job_id and item["status"] == "invalid_scope" for item in result)
    with connect() as con:
        row = con.execute("SELECT status,error_message FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
    assert row["status"] == "dead"
    assert "invalid_scope" in row["error_message"]


def test_vector_and_external_public_apis_reject_cross_owner_scope(monkeypatch, tmp_path):
    conversation_id = _seed_conversation_scope(monkeypatch, tmp_path, person_id="alice")
    from mlomega_audio_elite.governance_v18 import ScopeError
    from mlomega_audio_elite.vector_sync import sync_vectors
    from mlomega_audio_elite.external_memory import sync_external_all

    with pytest.raises(ScopeError):
        sync_vectors(conversation_id=conversation_id, person_id="bob")
    with pytest.raises(ScopeError):
        sync_external_all(conversation_id, person_id="bob")


def test_poststop_secondary_sync_uses_aligned_owner_aware_public_contracts(monkeypatch, tmp_path):
    conversation_id = _seed_conversation_scope(monkeypatch, tmp_path)
    import mlomega_audio_elite.vector_sync as vector_sync
    import mlomega_audio_elite.external_memory as external_memory
    from mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 import _sync_secondary_memory_for_conversation

    monkeypatch.setattr(
        vector_sync,
        "sync_vectors",
        lambda *, conversation_id, person_id, **_kwargs: {
            "status": "ok",
            "conversation_id": conversation_id,
            "person_id": person_id,
        },
    )
    monkeypatch.setattr(
        external_memory,
        "sync_external_all",
        lambda conversation_id, *, person_id: {
            "status": "ok",
            "conversation_id": conversation_id,
            "person_id": person_id,
        },
    )
    result = _sync_secondary_memory_for_conversation(conversation_id, person_id="me")
    assert result["status"] == "ok"
    assert result["steps"] == ["schedule", "vectors"]
    assert result["external_memory"]["status"] == "not_configured_core_profile"


def test_ingest_owner_resolution_is_not_the_last_speaker(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.ingest import _resolve_memory_owner
    from mlomega_audio_elite.governance_v18 import ScopeError

    owner, proof = _resolve_memory_owner(
        {"memory_owner_id": "will"}, ["will", "sarah"], {"S0": "will", "S1": "sarah"}
    )
    assert (owner, proof) == ("will", "metadata_owner")
    # The last mapping entry is Sarah. Legacy V17 accidentally used the last
    # loop variable; V18 keeps the explicit memory owner.
    owner, proof = _resolve_memory_owner({}, ["me", "sarah"], {"S0": "me", "S1": "sarah"})
    assert (owner, proof) == ("me", "legacy_unique_user_alias")
    with pytest.raises(ScopeError):
        _resolve_memory_owner({}, ["alice", "bob"], {"S0": "alice", "S1": "bob"})


def test_memory_revision_requires_explicit_owner(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.memory_correction import revise_memory
    from mlomega_audio_elite.governance_v18 import ScopeError

    with pytest.raises(ScopeError, match="explicit person_id"):
        revise_memory(
            target_table="memory_cards",
            target_id="missing",
            revision_type="invalidate",
            reason="test",
        )
