from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from mlomega_audio_elite.db import connect
from mlomega_audio_elite.governance_v18 import Scope, StageGateError, claim_work, ensure_v18_schema, finish_work


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")
    return root


def _write_transcript_capture(root: Path) -> Path:
    inbox = root / "inbox" / "transcripts"
    inbox.mkdir(parents=True, exist_ok=True)
    source = inbox / "session.json"
    source.write_text(
        json.dumps(
            {
                "turns": [
                    {
                        "text": "Je prépare le dossier de manière traçable.",
                        "speaker": {"label": "me", "person_id": "me", "confidence": 0.95},
                        "timestamp_start": "2026-06-21T10:00:00+00:00",
                        "timestamp_end": "2026-06-21T10:00:04+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    # This is deliberately named ``.json.json``: it used to be accidentally
    # treated as a second transcript and quarantined.
    sidecar = source.with_suffix(source.suffix + ".json")
    sidecar.write_text(
        json.dumps(
            {
                "captured_at": "2026-06-21T10:00:00+00:00",
                "source_device": "acceptance-test-device",
                "source_event_id": "acceptance-event-1",
                "sha256": digest,
            }
        ),
        encoding="utf-8",
    )
    # The production inbox deliberately waits for a legacy producer's write
    # to settle.  The acceptance fixture simulates a stable upload instead of
    # weakening that production protection.
    settled = time.time() - 5
    os.utime(source, (settled, settled))
    os.utime(sidecar, (settled, settled))
    return inbox


def _run_service_once(monkeypatch, tmp_path):
    root = _configure(monkeypatch, tmp_path)
    inbox = _write_transcript_capture(root)
    from mlomega_audio_elite.brainlive_service_v15_5 import (
        configure_brainlive_service,
        start_brainlive_service,
    )

    configure_brainlive_service(
        person_id="me",
        transcript_dir=str(inbox),
        sensor_tick_s=0.001,
        context_refresh_s=99999.0,
    )
    return start_brainlive_service(
        person_id="me",
        transcript_dir=str(inbox),
        max_iterations=1,
        post_stop_deep_flow=False,
    )


def test_final_service_sidecar_is_not_reingested_and_session_anchor_never_forms_a_bundle(monkeypatch, tmp_path):
    service = _run_service_once(monkeypatch, tmp_path)
    assert service["status"] == "completed"

    with connect() as con:
        turns = con.execute("SELECT * FROM brainlive_turn_buffer").fetchall()
        work = con.execute(
            "SELECT work_type,state FROM v18_work_leases ORDER BY work_type"
        ).fetchall()
        audit_paths = con.execute(
            "SELECT path FROM brainlive_service_processed_files ORDER BY path"
        ).fetchall()
    assert len(turns) == 1
    assert [(row["work_type"], row["state"]) for row in work] == [("inbox:transcript", "completed")]
    assert all(not str(row["path"]).endswith(".json.json") for row in audit_paths)

    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import run_brainlive_event_assembly

    assembled = run_brainlive_event_assembly(
        "me", package_date="2026-06-21", live_session_id=service["live_session_id"]
    )
    assert assembled["status"] == "ok"
    assert assembled["bundles"] == 1
    with connect() as con:
        bundle = con.execute(
            "SELECT transcript_json,raw_timeline_json FROM brainlive_event_bundles_v1514"
        ).fetchone()
    transcript = json.loads(bundle["transcript_json"])
    raw = json.loads(bundle["raw_timeline_json"])
    assert [turn["text"] for turn in transcript] == ["Je prépare le dossier de manière traçable."]
    assert all(ref["source_table"] != "brainlive_sessions" for ref in raw)


def test_final_poststop_no_llm_fails_closed_but_stubbed_orchestration_is_recoverable(monkeypatch, tmp_path):
    service = _run_service_once(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 import (
        post_stop_cleanup_eligible,
        run_brainlive_post_stop_deep_flow,
    )

    # No LLM may never masquerade as deep canonical evidence. The post-stop
    # manifest remains incomplete and cleanup is denied.
    blocked = run_brainlive_post_stop_deep_flow(
        person_id="me",
        live_session_id=service["live_session_id"],
        service_run_id=service["service_run_id"],
        package_date="2026-06-21",
        use_llm=False,
        run_deep_vision=False,
        run_silent_life=False,
    )
    assert blocked["status"] == "blocked"
    with pytest.raises(StageGateError):
        post_stop_cleanup_eligible(run_id=blocked["run_id"], person_id="me")

    # The orchestration itself is exercised without a remote LLM by stubbing
    # only the deep-engine contract. This still uses real service, assembly,
    # exports, stage ledger, manifests, cleanup gate and resume machinery.
    import mlomega_audio_elite.brain2_flow_v13_3 as brain2_flow
    import mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 as poststop

    monkeypatch.setattr(
        brain2_flow,
        "run_brain2_deep_stack_for_conversation",
        lambda conversation_id, *, person_id, **_kwargs: {
            "status": "ok",
            "conversation_id": conversation_id,
            "person_id": person_id,
            "deterministic_acceptance_stub": True,
        },
    )
    monkeypatch.setattr(
        poststop,
        "_sync_secondary_memory_for_conversation",
        lambda conversation_id, *, person_id: {
            "status": "ok",
            "conversation_id": conversation_id,
            "person_id": person_id,
            "deterministic_acceptance_stub": True,
        },
    )
    complete = run_brainlive_post_stop_deep_flow(
        person_id="me",
        live_session_id=service["live_session_id"],
        service_run_id="acceptance-stubbed-deep",
        package_date="2026-06-21",
        use_llm=True,
        run_deep_vision=False,
        run_silent_life=False,
        force=True,
    )
    assert complete["status"] == "completed"
    assert complete["exported_conversations"] == 1
    cleanup = post_stop_cleanup_eligible(run_id=complete["run_id"], person_id="me")
    assert cleanup["eligible"] is True


def test_final_concurrent_claim_has_one_winner_and_terminal_state_never_reopens(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    ensure_v18_schema()
    scope = Scope(person_id="me", live_session_id="session", mode="live")
    barrier = threading.Barrier(2)
    results: list[dict | None] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(
                claim_work(
                    work_type="acceptance:concurrent",
                    scope=scope,
                    source_key_value="source-event-1",
                    lease_seconds=30,
                    max_attempts=2,
                )
            )
        except BaseException as exc:  # pragma: no cover - report thread error directly
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert len(results) == 2
    winner = next(item for item in results if item is not None)
    assert sum(item is not None for item in results) == 1
    finish_work(
        work_key=winner["work_key"],
        lease_token=winner["lease_token"],
        status="completed",
        result={"acceptance": True},
    )
    assert (
        claim_work(
            work_type="acceptance:concurrent",
            scope=scope,
            source_key_value="source-event-1",
            lease_seconds=30,
            max_attempts=2,
        )
        is None
    )


def test_final_high_risk_entrypoints_refuse_implicit_owner(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import run_longitudinal_consolidation
    from mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 import run_brainlive_post_stop_deep_flow
    from mlomega_audio_elite.brainlive_service_v15_5 import configure_brainlive_service, start_brainlive_service
    from mlomega_audio_elite.governance_v18 import GovernanceError, ScopeError

    with pytest.raises(GovernanceError):
        configure_brainlive_service()
    with pytest.raises(GovernanceError):
        start_brainlive_service(max_iterations=1)
    with pytest.raises(StageGateError):
        run_brainlive_post_stop_deep_flow(use_llm=False)
    with pytest.raises(ScopeError):
        run_longitudinal_consolidation(use_llm=False)


def test_final_public_and_cli_owner_boundaries_fail_closed(monkeypatch, tmp_path):
    """No V18 production entrypoint may silently choose the oldest/newest user."""
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_sensor_fusion_v15_4 import configure_sensor_fusion
    from mlomega_audio_elite.brainlive_v15 import (
        list_live_inbox,
        record_user_disagreement,
        run_nightly_bridge,
        start_live_session,
    )
    from mlomega_audio_elite.governance_v18 import GovernanceError
    from mlomega_audio_elite import cli

    for call in (
        lambda: configure_sensor_fusion(),
        lambda: start_live_session(),
        lambda: list_live_inbox(),
        lambda: record_user_disagreement(None, "claim", "no"),
        lambda: run_nightly_bridge(),
    ):
        with pytest.raises(GovernanceError):
            call()
    with pytest.raises(SystemExit, match="explicit --person-id"):
        cli.main(["brain2-longitudinal-run", "--no-llm"])
