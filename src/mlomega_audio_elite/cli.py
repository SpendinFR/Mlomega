from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path

from .config import get_settings
from . import __version__
from .consolidation import consolidate_all
from .db import connect, init_db
from .ingest import ingest_audio, ingest_transcript_file
from .retrieval import answer
from .voice_identity import enroll_voice, match_voice


def _require_person_id(args) -> str:
    """Refuse ambiguous owner selection on V18 production commands.

    The old CLI silently substituted ``me``.  That is unsafe whenever a DB
    contains more than one profile, because different legacy modules selected
    different defaults.  Keep the compatibility option parser shape, but make
    command execution fail closed until the caller passes ``--person-id``.
    """
    value = getattr(args, "person_id", None)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit("V18 requires an explicit --person-id for this command")
    return value.strip()


def cmd_init_db(args) -> None:
    path = init_db(); print(f"DB initialisée: {path}")


def cmd_seed_example(args) -> None:
    init_db()
    path = Path(__file__).resolve().parents[2] / "examples" / "example_conversation.json"
    if not path.exists():
        path = Path.cwd() / "examples" / "example_conversation.json"
    conv_id = ingest_transcript_file(path)
    print(f"Exemple ingéré via stack élite complète: {conv_id}")


def cmd_ingest_transcript(args) -> None:
    conv_id = ingest_transcript_file(Path(args.path)); print(f"Conversation ingérée: {conv_id}")


def cmd_ingest_audio(args) -> None:
    conv_id = ingest_audio(Path(args.path), language=args.language, speaker_map_path=Path(args.speaker_map) if args.speaker_map else None); print(f"Audio ingéré: {conv_id}")


def cmd_query(args) -> None: print(answer(args.question))


def cmd_consolidate(args) -> None: print(json.dumps(consolidate_all(), ensure_ascii=False, indent=2))


def cmd_sync_vectors(args) -> None:
    from .vector_sync import sync_vectors
    print(json.dumps(sync_vectors(limit=args.limit, conversation_id=args.conversation_id, full=args.full, person_id=args.person_id), ensure_ascii=False, indent=2))


def cmd_sync_external(args) -> None:
    from .external_memory import sync_external_all
    print(json.dumps(sync_external_all(args.conversation_id, person_id=args.person_id), ensure_ascii=False, indent=2))


def cmd_graph(args) -> None:
    with connect() as con:
        rels = list(con.execute("""SELECT r.relation_type, ef.name AS from_name, et.name AS to_name, r.valid_from, r.valid_until, r.confidence
               FROM relations r JOIN entities ef ON ef.entity_id=r.from_entity_id JOIN entities et ON et.entity_id=r.to_entity_id
               ORDER BY r.created_at DESC LIMIT ?""", (args.limit,)))
    for r in rels: print(f"{r['from_name']} -[{r['relation_type']}]-> {r['to_name']} from={r['valid_from']} until={r['valid_until']} conf={r['confidence']:.2f}")


def cmd_timeline(args) -> None:
    with connect() as con:
        params=[]; sql="SELECT * FROM reflection_states"
        if args.topic: sql += " WHERE topic LIKE ?"; params.append(f"%{args.topic}%")
        sql += " ORDER BY period_start, created_at"; rows=list(con.execute(sql, params))
    if not rows: print("Aucun état de réflexion trouvé."); return
    for r in rows: print(f"{r['period_start'] or '?'} → {r['period_end'] or '?'} | {r['person_id']} | {r['topic']} | {r['stance']} | {r['summary']}")


def cmd_speakers(args) -> None:
    with connect() as con:
        rows=list(con.execute("SELECT * FROM speaker_profiles ORDER BY is_user DESC, display_name")); matches=list(con.execute("SELECT * FROM speaker_matches ORDER BY created_at DESC LIMIT 20"))
    print("Profils:")
    for r in rows: print(f"- {r['person_id']} | {r['display_name']} | is_user={bool(r['is_user'])}")
    print("Matches:")
    for m in matches: print(f"- {m['conversation_id']} {m['speaker_label']} -> {m['person_id']} conf={m['confidence']} method={m['method']}")



def cmd_memory_overview(args) -> None:
    from .memory_foundation import memory_overview
    with connect() as con:
        print(json.dumps(memory_overview(con), ensure_ascii=False, indent=2))


def cmd_memory_card(args) -> None:
    with connect() as con:
        row = con.execute("SELECT * FROM memory_cards WHERE card_id=? OR source_id=? ORDER BY updated_at DESC LIMIT 1", (args.id, args.id)).fetchone()
        if not row:
            print("Aucune memory_card trouvée.")
            return
        print(f"{row['card_id']} | {row['truth_status']} | {row['card_type']} | conf={row['confidence']}")
        print(f"Titre: {row['title']}")
        print(f"Résumé: {row['summary']}")
        print(f"Personne: {row['person_id']} | Sujet: {row['topic']} | Temps: {row['time_start']} → {row['time_end']}")
        print("Facettes:")
        for f in con.execute("SELECT facet_type, facet_value, confidence FROM memory_facets WHERE target_table='memory_cards' AND target_id=? ORDER BY weight DESC", (row['card_id'],)):
            print(f"- {f['facet_type']} = {f['facet_value']} conf={f['confidence']}")
        print("Preuves:")
        for e in con.execute("SELECT evidence_role, evidence_text, confidence FROM memory_evidence WHERE target_table='memory_cards' AND target_id=?", (row['card_id'],)):
            print(f"- {e['evidence_role']} conf={e['confidence']} :: {e['evidence_text']}")
        print("Liens sortants:")
        for l in con.execute("SELECT relation_type, to_table, to_id, confidence FROM memory_links WHERE from_table='memory_cards' AND from_id=? LIMIT 30", (row['card_id'],)):
            print(f"- {l['relation_type']} -> {l['to_table']}:{l['to_id']} conf={l['confidence']}")


def cmd_memory_revise(args) -> None:
    from .memory_correction import revise_memory
    patch = json.loads(args.patch) if args.patch else {}
    result = revise_memory(
        target_table=args.table,
        target_id=args.id,
        revision_type=args.type,
        reason=args.reason,
        patch=patch,
        confidence=args.confidence,
        person_id=args.person_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_sync_jobs(args) -> None:
    from .sync_jobs import list_sync_jobs
    print(json.dumps(list_sync_jobs(status=args.status, backend=args.backend, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_sync_pending(args) -> None:
    from .sync_jobs import run_pending_sync_jobs
    print(json.dumps(run_pending_sync_jobs(limit=args.limit, backend=args.backend), ensure_ascii=False, indent=2))


def cmd_mem0_config(args) -> None:
    from .local_mem0 import build_mem0_config
    config = build_mem0_config()
    vector_config = config.get("vector_store", {}).get("config", {})
    if vector_config.get("api_key"):
        vector_config["api_key"] = "***"
    print(json.dumps(config, ensure_ascii=False, indent=2, default=str))


def cmd_mem0_doctor(args) -> None:
    from .local_mem0 import build_mem0_config, create_mem0_memory
    settings = get_settings()
    config = build_mem0_config(settings)
    print("Mem0 local doctor")
    print(f"llm={settings.mem0_llm_provider}:{settings.mem0_llm_model}")
    print(f"embedder={settings.mem0_embedder_provider}:{settings.mem0_embedder_model} dims={settings.mem0_embedding_dims}")
    print(f"vector_store=qdrant url={settings.mem0_qdrant_url} collection={settings.mem0_qdrant_collection}")
    ok_all = True
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen(settings.ollama_base_url + "/api/tags", timeout=3) as r:
            tags = _json.loads(r.read().decode())
        model_names = {m.get("name") for m in tags.get("models", []) if isinstance(m, dict)}
        required = set()
        if settings.mem0_llm_provider.lower() == "ollama":
            required.add(settings.mem0_llm_model)
        if settings.mem0_embedder_provider.lower() == "ollama":
            required.add(settings.mem0_embedder_model)
        missing = sorted(required - model_names)
        if missing:
            raise RuntimeError("modèles Ollama manquants pour Mem0: " + ", ".join(missing))
        print("✅ Ollama modèles Mem0 présents")
    except Exception as exc:
        print(f"❌ Ollama/Mem0 models: {str(exc)[:160]}"); ok_all = False
    try:
        from qdrant_client import QdrantClient
        QdrantClient(url=settings.mem0_qdrant_url).get_collections()
        print("✅ Qdrant Mem0 accessible")
    except Exception as exc:
        print(f"❌ Qdrant Mem0: {str(exc)[:160]}"); ok_all = False
    try:
        _ = create_mem0_memory()
        print("✅ Memory.from_config(config local) OK")
    except Exception as exc:
        print(f"❌ Mem0 local config/init: {str(exc)[:220]}"); ok_all = False
        if args.show_config:
            print(json.dumps(config, ensure_ascii=False, indent=2, default=str))
    if not ok_all and args.fail:
        raise SystemExit(2)


def cmd_enroll_voice(args) -> None:
    emb_id = enroll_voice(args.person_id, Path(args.path), display_name=args.display_name, is_user=args.is_user); print(f"Voix enrôlée: {emb_id}")


def cmd_match_voice(args) -> None: print(json.dumps(match_voice(Path(args.path)), ensure_ascii=False, indent=2))


def cmd_setup_me(args) -> None:
    from .voice_learning import setup_me
    print(json.dumps(setup_me(Path(args.path), display_name=args.display_name, person_id=args.person_id), ensure_ascii=False, indent=2))


def cmd_voice_pending(args) -> None:
    from .voice_learning import pending_unknown_voices
    print(json.dumps(pending_unknown_voices(), ensure_ascii=False, indent=2))


def cmd_name_voice(args) -> None:
    from .voice_learning import name_unknown_voice
    print(json.dumps(name_unknown_voice(args.cluster_id, args.person_id, display_name=args.display_name, is_user=args.is_user), ensure_ascii=False, indent=2))


def cmd_preprocess_audio(args) -> None:
    from .audio_preprocess import preprocess_audio
    print(json.dumps(preprocess_audio(Path(args.path), remove_silence=args.remove_silence and not args.keep_silence, max_chunk_seconds=args.max_chunk_seconds, silence_threshold_db=args.silence_threshold_db, min_silence_seconds=args.min_silence_seconds), ensure_ascii=False, indent=2))


def cmd_flow_once(args) -> None:
    from .brain2_flow_v13_3 import process_incoming_path
    print(json.dumps(process_incoming_path(Path(args.path), run_v13=not args.no_v13, preprocess_long_audio=not args.no_preprocess, max_chunk_seconds=args.max_chunk_seconds), ensure_ascii=False, indent=2))


def cmd_flow_watch(args) -> None:
    from .brain2_flow_v13_3 import watch_inbox
    print(json.dumps(watch_inbox(audio_dir=Path(args.audio_dir) if args.audio_dir else None, transcript_dir=Path(args.transcript_dir) if args.transcript_dir else None, poll_seconds=args.poll_seconds, once=args.once, run_v13=not args.no_v13), ensure_ascii=False, indent=2))


def cmd_v13_subtopics(args) -> None:
    from .brain2_flow_v13_3 import build_subtopic_segments
    print(json.dumps(build_subtopic_segments(args.conversation_id), ensure_ascii=False, indent=2))


def cmd_v13_discover_outcomes(args) -> None:
    from .brain2_flow_v13_3 import discover_latent_outcomes_from_conversation
    print(json.dumps(discover_latent_outcomes_from_conversation(args.conversation_id, limit_pending=args.limit_pending), ensure_ascii=False, indent=2))


def cmd_v12_build(args) -> None:
    from .behavior_v12 import build_v12_all, build_v12_for_conversation
    result = build_v12_for_conversation(args.conversation_id) if args.conversation_id else build_v12_all()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_v12_overview(args) -> None:
    from .behavior_v12 import v12_overview
    print(json.dumps(v12_overview(), ensure_ascii=False, indent=2))


def cmd_v12_predict(args) -> None:
    from .behavior_v12 import predict
    print(json.dumps(predict(args.target, args.context, person_id=args.person_id, horizon=args.horizon), ensure_ascii=False, indent=2))


def cmd_v12_verify(args) -> None:
    from .behavior_v12 import verify_prediction
    print(json.dumps(verify_prediction(args.prediction_id, args.observed, match_score=args.match_score, note=args.note), ensure_ascii=False, indent=2))



def cmd_v13_audit_plan(args) -> None:
    from .behavior_v13 import audit_v13_plan
    print(json.dumps(audit_v13_plan(persist=True), ensure_ascii=False, indent=2))


def cmd_v13_build(args) -> None:
    from .behavior_v13 import build_v13_all, build_v13_for_conversation
    result = build_v13_for_conversation(args.conversation_id, max_episodes=args.max_episodes) if args.conversation_id else build_v13_all(max_episodes_per_conversation=args.max_episodes)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_v13_overview(args) -> None:
    from .behavior_v13 import v13_overview
    print(json.dumps(v13_overview(), ensure_ascii=False, indent=2))


def cmd_v13_predict(args) -> None:
    from .behavior_v13 import predict_v13
    print(json.dumps(predict_v13(args.target, args.context, person_id=args.person_id, horizon=args.horizon), ensure_ascii=False, indent=2))


def cmd_v13_verify(args) -> None:
    from .behavior_v13 import verify_v13_prediction
    print(json.dumps(verify_v13_prediction(args.prediction_id, args.observed, match_score=args.match_score, note=args.note), ensure_ascii=False, indent=2))


def cmd_doctor(args) -> None:
    settings=get_settings(); print(f"home={settings.root_dir}"); print(f"db={settings.db_path} exists={settings.db_path.exists()}"); print(f"raw={settings.raw_dir}")
    with connect() as con:
        for table in ["conversations","turns","source_spans","extraction_runs","memory_cards","memory_frames","memory_evidence","memory_facets","memory_links","atomic_memories","relations","reflection_states","patterns","self_model_facts","retrieval_chunks","sync_jobs","memory_revisions","episodes","speech_acts","internal_state_snapshots","thought_hypotheses","action_intentions","choice_episodes","prediction_cases","predictions","calibration_scores","v13_cognitive_cycles","v13_llm_extractions","v13_dynamic_models","v13_prediction_explanations","v13_replay_events","v13_intervention_plans"]:
            try: count=con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            except Exception: count="missing"
            print(f"{table}: {count}")


def _check_module(mod: str) -> tuple[bool, str]:
    try:
        importlib.import_module(mod); return True, "ok"
    except Exception as exc:
        return False, str(exc)[:140]


def cmd_integrity_v176_migrate(args) -> None:
    # The integrity migration attaches constraints to BrainLive tables.  Creating
    # only the base DB would report a false-success on a fresh installation.
    from .brainlive_v15 import ensure_brainlive_schema
    ensure_brainlive_schema()
    print(json.dumps({"status": "ok", "migration": "v176_integrity_kernel", "brainlive_schema": "ensured"}, ensure_ascii=False, indent=2))


def cmd_v18_legacy_migrate(args) -> None:
    from .v18_migration import run_legacy_migration
    print(json.dumps(
        run_legacy_migration(
            requested_person_id=args.person_id,
            apply=bool(args.apply),
            limit=args.limit,
        ),
        ensure_ascii=False, indent=2,
    ))


def cmd_v18_legacy_forecast_reconcile(args) -> None:
    from .v18_legacy_forecasts import reconcile_legacy_forecasts
    from .db import connect
    with connect() as con:
        result = reconcile_legacy_forecasts(person_id=_require_person_id(args), con=con)
        con.commit()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_v18_legacy_forecast_outcome(args) -> None:
    from .v18_legacy_forecasts import record_legacy_forecast_outcome
    correct = True if args.correct else (False if args.incorrect else None)
    print(json.dumps(record_legacy_forecast_outcome(
        source_table=args.source_table,
        source_id=args.source_id,
        person_id=_require_person_id(args),
        correct=correct,
        evidence=json.loads(args.evidence) if args.evidence else None,
    ), ensure_ascii=False, indent=2))


def cmd_v18_legacy_forecast_audit(args) -> None:
    from .v18_legacy_forecasts import legacy_forecast_audit
    print(json.dumps(legacy_forecast_audit(person_id=_require_person_id(args)), ensure_ascii=False, indent=2))


def cmd_v18_poststop_cleanup_check(args) -> None:
    from .brainlive_poststop_deep_flow_v15_15 import post_stop_cleanup_eligible
    print(json.dumps(
        post_stop_cleanup_eligible(run_id=args.run_id, person_id=args.person_id),
        ensure_ascii=False, indent=2,
    ))

def cmd_v18_release_audit(args) -> None:
    from .v18_release_audit import audit_v18_release
    report = audit_v18_release(
        stale_after_seconds=args.stale_after_seconds,
        strict=bool(args.strict),
        persist=True,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if getattr(args, "fail", False) and report.get("status") != "ok":
        raise SystemExit(2)



def cmd_integrity_v176_audit(args) -> None:
    from .brainlive_v15 import ensure_brainlive_schema
    from .integrity_v176 import integrity_audit_v176
    ensure_brainlive_schema()
    report = integrity_audit_v176()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if getattr(args, "fail", False) and report.get("status") != "ok":
        raise SystemExit(2)



def cmd_doctor_core_v187(args) -> None:
    from .operations_v18_8 import core_doctor
    report = core_doctor(
        check_services=not bool(args.no_services),
        check_models=bool(args.check_models),
        check_bridge=bool(args.check_bridge or getattr(args, "check_bridge_delivery", False)),
        check_bridge_delivery=bool(getattr(args, "check_bridge_delivery", False)),
        check_vectors=bool(args.check_vectors),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if bool(args.fail) and report.get("status") != "ok":
        raise SystemExit(2)


def cmd_resume_close_day_v187(args) -> None:
    from .operations_v18_8 import resume_close_day
    print(json.dumps(resume_close_day(person_id=_require_person_id(args), package_date=args.package_date, force=bool(args.force)), ensure_ascii=False, indent=2))


def cmd_recovery_status_v187(args) -> None:
    from .operations_v18_8 import recovery_status
    print(json.dumps(recovery_status(person_id=_require_person_id(args)), ensure_ascii=False, indent=2))


def cmd_runtime_status_v187(args) -> None:
    from .operations_v18_8 import runtime_status
    print(json.dumps(runtime_status(), ensure_ascii=False, indent=2))


def cmd_recover_stale_services_v187(args) -> None:
    from .brainlive_service_v15_5 import recover_stale_brainlive_service_runs
    print(json.dumps(recover_stale_brainlive_service_runs(stale_after_s=args.stale_after_seconds), ensure_ascii=False, indent=2))

def cmd_resume_inbox_drain_v187(args) -> None:
    from .brainlive_service_v15_5 import resume_brainlive_pending_ingest
    print(json.dumps(resume_brainlive_pending_ingest(
        person_id=_require_person_id(args),
        live_session_id=args.live_session_id,
        service_run_id=args.service_run_id,
    ), ensure_ascii=False, indent=2))

def cmd_doctor_elite(args) -> None:
    settings=get_settings()
    # Compatibility command retained for V17 guides. In the supported core
    # profile, it must not demand legacy Neo4j/Graphiti/Mem0 dependencies.
    if str(getattr(settings, "deployment_profile", "")).upper().startswith(("CORE_BRAINLIVE_V18_7", "CORE_BRAINLIVE_V18_8")):
        from .operations_v18_8 import core_doctor
        print(f"MLOmega V{__version__} — doctor-elite compatibility alias -> doctor-core-v18-8")
        report = core_doctor(check_services=True, check_models=False, check_bridge=False, check_bridge_delivery=False, check_vectors=False)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if bool(getattr(args, "fail", False)) and report.get("status") != "ok":
            raise SystemExit(2)
        return
    print(f"MemoryLight Omega Audio Elite V{__version__} — doctor RTX")
    print(f"strict_elite={settings.strict_elite} cuda_device={settings.whisperx_device}")
    ok_all=True
    if not settings.strict_elite:
        print("❌ MLOMEGA_STRICT_ELITE doit être true dans cette build.")
        ok_all=False
    checks = {
        "whisperx": "WhisperX transcription + word timestamps",
        "pyannote.audio": "pyannote diarization",
        "speechbrain": "SpeechBrain ECAPA voice identity",
        "torch": "PyTorch CUDA",
        "torchaudio": "audio IO/resample",
        "sentence_transformers": "Qwen/BGE embeddings + reranker",
        "qdrant_client": "Qdrant vector DB",
        "neo4j": "Neo4j driver for Graphiti/projection",
        "graphiti_core": "Graphiti temporal graph",
        "mem0": "Mem0 memory layer",
        "ollama": "Ollama Python client / service local Qwen obligatoire",
    }
    for mod, desc in checks.items():
        ok,msg=_check_module(mod)
        ok_all = ok_all and ok
        print(("✅" if ok else "❌") + f" {mod}: {desc} — {msg}")
    if settings.enable_pyannote:
        token_ok = bool(settings.hf_token) and not str(settings.hf_token).startswith("YOUR_HUGGINGFACE")
        print(("✅" if token_ok else "❌") + " HuggingFace token pyannote: " + ("présent" if token_ok else "absent/placeholder"))
        ok_all = ok_all and token_ok
    try:
        import torch
        print(f"CUDA available={torch.cuda.is_available()} device_count={torch.cuda.device_count()}")
        if torch.cuda.is_available(): print(f"GPU={torch.cuda.get_device_name(0)}")
        ok_all = ok_all and torch.cuda.is_available()
    except Exception as exc:
        print(f"❌ torch cuda check: {exc}"); ok_all=False
    # service pings
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen(settings.ollama_base_url + "/api/tags", timeout=3) as r:
            tags = _json.loads(r.read().decode())
        model_names = {m.get("name") for m in tags.get("models", []) if isinstance(m, dict)}
        required_models = {settings.ollama_model}
        if settings.mem0_llm_provider.lower() == "ollama":
            required_models.add(settings.mem0_llm_model)
        if settings.mem0_embedder_provider.lower() == "ollama":
            required_models.add(settings.mem0_embedder_model)
        missing_models = sorted(required_models - model_names)
        if missing_models:
            raise RuntimeError("modèles absents; lance: " + " && ".join(f"ollama pull {m}" for m in missing_models))
        print(f"✅ Ollama service/models: {settings.ollama_base_url} {', '.join(sorted(required_models))}")
        print(f"✅ Mem0 local: llm={settings.mem0_llm_provider}:{settings.mem0_llm_model} embedder={settings.mem0_embedder_provider}:{settings.mem0_embedder_model} qdrant={settings.mem0_qdrant_collection}")
    except Exception as exc:
        print(f"❌ Ollama service: {settings.ollama_base_url} — {str(exc)[:120]}"); ok_all=False
    try:
        from qdrant_client import QdrantClient
        QdrantClient(url=settings.qdrant_url).get_collections()
        print(f"✅ Qdrant service: {settings.qdrant_url}")
    except Exception as exc:
        print(f"❌ Qdrant service: {settings.qdrant_url} — {str(exc)[:120]}"); ok_all=False
    if ok_all: print("OUI — stack élite détectée.")
    else:
        print("Stack élite incomplète sur cette machine. Sous Windows lance scripts\\windows_install_all.ps1, ou sous Linux scripts/install_rtx3070_ubuntu.sh puis docker compose up -d qdrant neo4j.")
        if args.fail: raise SystemExit(2)



def cmd_v13_autonomous(args) -> None:
    from .autonomous_v13_4 import run_autonomous_insights
    print(json.dumps(run_autonomous_insights(args.conversation_id, trigger_type="manual"), ensure_ascii=False, indent=2))

def cmd_v13_insights(args) -> None:
    from .autonomous_v13_4 import list_autonomous_insights
    print(json.dumps(list_autonomous_insights(status=args.status, limit=args.limit), ensure_ascii=False, indent=2))

def cmd_v13_ask(args) -> None:
    from .autonomous_v13_4 import ask_life
    print(json.dumps(ask_life(args.question, person_id=args.person_id), ensure_ascii=False, indent=2))

def cmd_v14_audit(args) -> None:
    from .pattern_mirror_v14 import audit_v14
    print(json.dumps(audit_v14(persist=True), ensure_ascii=False, indent=2))


def cmd_v14_mirror(args) -> None:
    from .pattern_mirror_v14 import run_pattern_mirror, run_pattern_mirror_all
    person_id = _require_person_id(args)
    result = run_pattern_mirror(args.conversation_id, person_id=person_id, trigger_type="manual", scope=args.scope) if args.conversation_id else run_pattern_mirror_all(person_id=person_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_v14_insights(args) -> None:
    from .pattern_mirror_v14 import list_pattern_mirror_cards
    print(json.dumps(list_pattern_mirror_cards(person_id=args.person_id, status=args.status, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_digest(args) -> None:
    from .pattern_mirror_v14 import pattern_mirror_digest
    print(json.dumps(pattern_mirror_digest(person_id=args.person_id, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_ask(args) -> None:
    from .brain2_router_v14_2 import ask_brain2
    print(json.dumps(ask_brain2(args.question, person_id=args.person_id), ensure_ascii=False, indent=2))



def cmd_v14_route(args) -> None:
    from .brain2_router_v14_2 import route_question
    print(json.dumps(route_question(args.question, person_id=args.person_id), ensure_ascii=False, indent=2))


def cmd_v14_select(args) -> None:
    from .brain2_router_v14_2 import select_candidates
    print(json.dumps(select_candidates(args.question, person_id=args.person_id, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_1_audit(args) -> None:
    from .brain2_router_v14_1 import audit_v14_1
    print(json.dumps(audit_v14_1(persist=True), ensure_ascii=False, indent=2))

def cmd_v14_2_audit(args) -> None:
    from .brain2_router_v14_2 import audit_v14_2
    print(json.dumps(audit_v14_2(persist=True), ensure_ascii=False, indent=2))


def cmd_v14_3_audit(args) -> None:
    from .self_model_export_v14_3 import audit_v14_3
    print(json.dumps(audit_v14_3(persist=True), ensure_ascii=False, indent=2))


def cmd_v14_4_audit(args) -> None:
    from .auto_verification_v14_4 import audit_v14_4
    print(json.dumps(audit_v14_4(persist=True), ensure_ascii=False, indent=2))


def cmd_v14_auto_verify(args) -> None:
    from .auto_verification_v14_4 import auto_verify_latent_outcome_predictions
    print(json.dumps(auto_verify_latent_outcome_predictions(conversation_id=args.conversation_id, person_id=args.person_id, limit=args.limit, min_confidence=args.min_confidence, skip_already_verified=not args.allow_existing_results), ensure_ascii=False, indent=2))


def cmd_v14_5_audit(args) -> None:
    from .people_openloops_v14_5 import audit_v14_5
    print(json.dumps(audit_v14_5(persist=True), ensure_ascii=False, indent=2))


def cmd_v14_5_run(args) -> None:
    from .people_openloops_v14_5 import run_v14_5_post_conversation
    print(json.dumps(run_v14_5_post_conversation(args.conversation_id, person_id=args.person_id), ensure_ascii=False, indent=2))


def cmd_v14_people_hypotheses(args) -> None:
    from .people_openloops_v14_5 import list_people_identity_hypotheses
    print(json.dumps(list_people_identity_hypotheses(status=args.status, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_open_loops(args) -> None:
    from .people_openloops_v14_5 import list_personal_open_loops
    print(json.dumps(list_personal_open_loops(person_id=args.person_id, status=args.status, limit=args.limit), ensure_ascii=False, indent=2))



def cmd_v14_6_audit(args) -> None:
    from .interpersonal_state_v14_6 import audit_v14_6
    print(json.dumps(audit_v14_6(persist=True), ensure_ascii=False, indent=2))


def cmd_v14_6_run(args) -> None:
    from .interpersonal_state_v14_6 import run_v14_6_post_conversation
    print(json.dumps(run_v14_6_post_conversation(args.conversation_id, person_id=args.person_id), ensure_ascii=False, indent=2))


def cmd_v14_people_models(args) -> None:
    from .interpersonal_state_v14_6 import list_other_person_models
    print(json.dumps(list_other_person_models(person_id=args.person_id, person_hint=args.person_hint, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_social_aftereffects(args) -> None:
    from .interpersonal_state_v14_6 import list_social_aftereffects
    print(json.dumps(list_social_aftereffects(person_id=args.person_id, status=args.status, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_7_audit(args) -> None:
    from .proactive_interventions_v14_7 import audit_v14_7
    print(json.dumps(audit_v14_7(persist=True), ensure_ascii=False, indent=2))


def cmd_v14_proactive_run(args) -> None:
    from .proactive_interventions_v14_7 import run_proactive_interventions
    print(json.dumps(run_proactive_interventions(args.conversation_id, person_id=args.person_id, trigger_type=args.trigger_type, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_interventions(args) -> None:
    from .proactive_interventions_v14_7 import list_intervention_inbox
    print(json.dumps(list_intervention_inbox(person_id=_require_person_id(args), status=args.status, priority=args.priority, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_intervention_feedback(args) -> None:
    from .proactive_interventions_v14_7 import record_intervention_feedback
    print(json.dumps(record_intervention_feedback(args.queue_id, person_id=args.person_id, feedback_type=args.type, note=args.note, helpfulness=args.helpfulness, action_taken=args.action_taken), ensure_ascii=False, indent=2))


def cmd_v14_intervention_export(args) -> None:
    from .proactive_interventions_v14_7 import export_intervention_inbox
    print(json.dumps(export_intervention_inbox(person_id=args.person_id, output_dir=Path(args.output_dir) if args.output_dir else None, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_intervention_policy(args) -> None:
    from .proactive_interventions_v14_7 import get_intervention_policy, update_intervention_policy
    patch = json.loads(args.patch) if args.patch else None
    result = update_intervention_policy(args.person_id, patch=patch) if patch else get_intervention_policy(args.person_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))



def cmd_v14_8_audit(args) -> None:
    from .clarification_inbox_v14_8 import audit_v14_8
    print(json.dumps(audit_v14_8(persist=True), ensure_ascii=False, indent=2))


def cmd_v14_clarification_run(args) -> None:
    from .clarification_inbox_v14_8 import run_clarification_inbox
    print(json.dumps(run_clarification_inbox(args.conversation_id, person_id=args.person_id, trigger_type=args.trigger_type, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_clarifications(args) -> None:
    from .clarification_inbox_v14_8 import list_clarifications
    print(json.dumps(list_clarifications(person_id=args.person_id, status=args.status, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_answer(args) -> None:
    from .clarification_inbox_v14_8 import answer_clarification
    print(json.dumps(answer_clarification(args.item_id, args.answer, person_id=_require_person_id(args)), ensure_ascii=False, indent=2))


def cmd_v14_clarification_export(args) -> None:
    from .clarification_inbox_v14_8 import export_clarification_inbox
    print(json.dumps(export_clarification_inbox(person_id=args.person_id, output_dir=Path(args.output_dir) if args.output_dir else None, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_clarification_policy(args) -> None:
    from .clarification_inbox_v14_8 import get_clarification_policy, update_clarification_policy
    patch = json.loads(args.patch) if args.patch else None
    result = update_clarification_policy(_require_person_id(args), patch=patch) if patch else get_clarification_policy(_require_person_id(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_v14_autopilot_coverage(args) -> None:
    from .auto_verification_v14_4 import autopilot_coverage
    print(json.dumps(autopilot_coverage(), ensure_ascii=False, indent=2))


def cmd_v14_auto_consolidate(args) -> None:
    from .self_model_export_v14_3 import run_due_periodic_consolidations
    periods = args.periods.split(",") if args.periods else None
    print(json.dumps(run_due_periodic_consolidations(person_id=args.person_id, periods=periods, force=args.force, export_after=not args.no_export), ensure_ascii=False, indent=2))


def cmd_v14_scheduler_status(args) -> None:
    from .self_model_export_v14_3 import scheduler_status
    print(json.dumps(scheduler_status(person_id=args.person_id), ensure_ascii=False, indent=2))


def cmd_export_self_model(args) -> None:
    from .self_model_export_v14_3 import export_self_model
    print(json.dumps(export_self_model(person_id=args.person_id, fmt=args.format, scope=args.scope, output_dir=Path(args.output_dir) if args.output_dir else None, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_self_model(args) -> None:
    from .self_model_export_v14_3 import collect_self_model_bundle
    print(json.dumps(collect_self_model_bundle(person_id=args.person_id, limit=args.limit), ensure_ascii=False, indent=2))

def cmd_v14_consolidate(args) -> None:
    from .pattern_mirror_v14 import run_periodic_mirror
    print(json.dumps(run_periodic_mirror(person_id=args.person_id, period=args.period, period_start=args.start, period_end=args.end), ensure_ascii=False, indent=2))


def cmd_v14_snapshots(args) -> None:
    from .pattern_mirror_v14 import list_periodic_snapshots
    print(json.dumps(list_periodic_snapshots(person_id=args.person_id, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_v14_today(args) -> None:
    from .pattern_mirror_v14 import run_periodic_mirror
    print(json.dumps(run_periodic_mirror(person_id=args.person_id, period="day"), ensure_ascii=False, indent=2))



# =========================
# V15 BrainLive + Vision
# =========================

def cmd_brainlive_audit(args) -> None:
    from .brainlive_v15 import audit_brainlive
    print(json.dumps(audit_brainlive(persist=True), ensure_ascii=False, indent=2))


def cmd_brainlive_start(args) -> None:
    from .brainlive_v15 import start_live_session
    active_people = json.loads(args.active_people) if args.active_people else []
    print(json.dumps(start_live_session(person_id=args.person_id, title=args.title, active_people=active_people, location_hint=args.location_hint, mode=args.mode), ensure_ascii=False, indent=2))


def cmd_brainlive_end(args) -> None:
    from .brainlive_v15 import end_live_session
    print(json.dumps(end_live_session(args.live_session_id, notes=args.notes), ensure_ascii=False, indent=2))


def cmd_brainlive_turn(args) -> None:
    from .brainlive_v15 import ingest_live_turn
    metadata = json.loads(args.metadata) if args.metadata else {}
    print(json.dumps(ingest_live_turn(args.live_session_id, args.text, speaker_label=args.speaker_label, speaker_person_id=args.speaker_person_id, speaker_confidence=args.speaker_confidence, is_final=not args.partial, timestamp_start=args.timestamp_start, timestamp_end=args.timestamp_end, metadata=metadata), ensure_ascii=False, indent=2))


def cmd_brainlive_context(args) -> None:
    from .brainlive_v15 import build_active_context
    active_people = json.loads(args.active_people) if args.active_people else None
    result = build_active_context(args.live_session_id, active_people=active_people, limit=args.limit)
    if args.full:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        compact = {"active_context_id": result["active_context_id"], "session": result["context"].get("session"), "recent_turns_summary": result["context"].get("recent_turns_summary"), "visual_context": result["context"].get("visual_context"), "brain2_context_keys": list((result["context"].get("brain2_context") or {}).keys())}
        print(json.dumps(compact, ensure_ascii=False, indent=2))


def cmd_brainlive_run(args) -> None:
    from .brainlive_v15 import run_brainlive
    active_people = json.loads(args.active_people) if args.active_people else None
    print(json.dumps(run_brainlive(args.live_session_id, mode=args.mode, use_llm=not args.no_llm, timeout=args.timeout, active_people=active_people, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_brainlive_inbox(args) -> None:
    from .brainlive_v15 import list_live_inbox
    print(json.dumps(list_live_inbox(person_id=args.person_id, status=args.status, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_brainlive_outcome(args) -> None:
    from .brainlive_v15 import record_prediction_outcome
    print(json.dumps(record_prediction_outcome(args.forecast_id, args.observed_after, person_id=args.person_id, live_session_id=args.live_session_id, candidate_id=args.candidate_id, match_score=args.match_score, user_feedback=args.user_feedback), ensure_ascii=False, indent=2))


def cmd_brainlive_disagree(args) -> None:
    from .brainlive_v15 import record_user_disagreement
    print(json.dumps(record_user_disagreement(args.candidate_id, args.system_claim, args.user_response, person_id=args.person_id, live_session_id=args.live_session_id), ensure_ascii=False, indent=2))


def cmd_brainlive_nightly(args) -> None:
    from .brainlive_v15 import run_nightly_bridge
    print(json.dumps(run_nightly_bridge(person_id=args.person_id, run_date=args.run_date, force=args.force), ensure_ascii=False, indent=2))



def cmd_vision_ingest_frame(args) -> None:
    from .brainlive_v15 import ingest_vision_frame
    obs = None
    if args.observation_json:
        obs = json.loads(args.observation_json)
    elif args.observation_file:
        obs = json.loads(Path(args.observation_file).read_text(encoding="utf-8"))
    print(json.dumps(ingest_vision_frame(Path(args.path), live_session_id=args.live_session_id, conversation_id=args.conversation_id, captured_at=args.captured_at, device_source=args.device_source, observation=obs, model=args.model), ensure_ascii=False, indent=2))


def cmd_brainlive_longitudinal_audit(args) -> None:
    from .brainlive_longitudinal_v15_1 import audit_longitudinal
    print(json.dumps(audit_longitudinal(), ensure_ascii=False, indent=2))


def cmd_brainlive_mine_routines(args) -> None:
    from .brainlive_longitudinal_v15_1 import mine_routines
    print(json.dumps(mine_routines(person_id=args.person_id, start_time=args.start, end_time=args.end, min_support=args.min_support, use_llm=not args.no_llm, timeout=args.timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_hypotheses_run(args) -> None:
    from .brainlive_longitudinal_v15_1 import run_hypothesis_engine
    routines = args.routine_id or None
    print(json.dumps(run_hypothesis_engine(person_id=args.person_id, routine_ids=routines, timeout=args.timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_outcomes_auto(args) -> None:
    from .brainlive_longitudinal_v15_1 import evaluate_outcomes_auto
    print(json.dumps(evaluate_outcomes_auto(person_id=args.person_id, limit=args.limit, timeout=args.timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_interpret_disagreement(args) -> None:
    from .brainlive_longitudinal_v15_1 import interpret_disagreement_llm
    print(json.dumps(interpret_disagreement_llm(args.disagreement_id, timeout=args.timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_match_affordances(args) -> None:
    from .brainlive_longitudinal_v15_1 import match_personal_affordances
    print(json.dumps(match_personal_affordances(live_session_id=args.live_session_id, timeout=args.timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_scheduler_config(args) -> None:
    from .brainlive_longitudinal_v15_1 import configure_daily_nightly_scheduler
    periods = args.brain2_periods.split(",") if args.brain2_periods else None
    print(json.dumps(configure_daily_nightly_scheduler(person_id=args.person_id, timezone_name=args.timezone, daytime_tick_minutes=args.daytime_tick_minutes, nightly_time=args.nightly_time, brain2_periods=periods), ensure_ascii=False, indent=2))


def cmd_brainlive_scheduler_tick(args) -> None:
    from .brainlive_longitudinal_v15_1 import scheduler_tick
    print(json.dumps(scheduler_tick(person_id=args.person_id, kind=args.kind, live_session_id=args.live_session_id, run_date=args.run_date, timeout=args.timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_replay(args) -> None:
    from .brainlive_longitudinal_v15_1 import replay_offline
    print(json.dumps(replay_offline(person_id=args.person_id, conversation_id=args.conversation_id, start_time=args.start, end_time=args.end, step_turns=args.step_turns, timeout=args.timeout), ensure_ascii=False, indent=2))



def cmd_brainlive_realtime_audit(args) -> None:
    from .brainlive_realtime_v15_2 import realtime_audit
    print(json.dumps(realtime_audit(), ensure_ascii=False, indent=2))


def cmd_brainlive_runtime_profile(args) -> None:
    from .brainlive_realtime_v15_2 import configure_runtime_profile
    print(json.dumps(configure_runtime_profile(person_id=_require_person_id(args), h0_timeout=args.h0_timeout, h1_timeout=args.h1_timeout, h2_timeout=args.h2_timeout, vlm_timeout=args.vlm_timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_live_image(args) -> None:
    from .brainlive_realtime_v15_2 import ingest_live_image
    print(json.dumps(ingest_live_image(args.live_session_id, args.path, device_source=args.device_source, use_vlm=not args.no_vlm, model=args.model, timeout=args.timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_live_tick(args) -> None:
    from .brainlive_realtime_v15_2 import live_tick
    print(json.dumps(live_tick(args.live_session_id, horizon=args.horizon, text=args.text, image_path=args.image, audio_sample_path=args.audio_sample, speaker_label=args.speaker_label, speaker_person_id=args.speaker_person_id, speaker_confidence=args.speaker_confidence, location_hint=args.location_hint, use_vlm=not args.no_vlm, use_llm=not args.no_llm, timeout=args.timeout), ensure_ascii=False, indent=2))


def cmd_brainlive_live_cycle(args) -> None:
    from .brainlive_realtime_v15_2 import live_cycle_all_horizons
    print(json.dumps(live_cycle_all_horizons(args.live_session_id, text=args.text, image_path=args.image, audio_sample_path=args.audio_sample, speaker_label=args.speaker_label, speaker_person_id=args.speaker_person_id, location_hint=args.location_hint, use_vlm=not args.no_vlm, use_llm=not args.no_llm), ensure_ascii=False, indent=2))


def cmd_brainlive_daemon_audit(args) -> None:
    from .brainlive_daemon_v15_3 import daemon_audit
    print(json.dumps(daemon_audit(), ensure_ascii=False, indent=2))


def cmd_brainlive_daemon_config(args) -> None:
    from .brainlive_daemon_v15_3 import configure_daemon
    print(json.dumps(configure_daemon(
        person_id=_require_person_id(args),
        transcript_watch_dir=args.transcript_dir,
        image_watch_dir=args.image_dir,
        audio_watch_dir=args.audio_dir,
        vad_backend=args.vad_backend,
        asr_backend=args.asr_backend,
        h1_interval_s=args.h1_interval,
        h2_interval_s=args.h2_interval,
        vision_interval_s=args.vision_interval,
        active_context_refresh_s=args.context_refresh,
        outcome_watch_interval_s=args.outcome_interval,
        sleep_s=args.sleep,
        max_iterations=args.max_iterations,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_daemon_iteration(args) -> None:
    from .brainlive_daemon_v15_3 import daemon_iteration
    print(json.dumps(daemon_iteration(args.live_session_id, config_id=args.config_id, person_id=_require_person_id(args), use_llm=not args.no_llm, use_vlm=not args.no_vlm), ensure_ascii=False, indent=2))


def cmd_brainlive_daemon_run(args) -> None:
    from .brainlive_daemon_v15_3 import run_daemon
    print(json.dumps(run_daemon(
        live_session_id=args.live_session_id,
        person_id=_require_person_id(args),
        config_id=args.config_id,
        title=args.title,
        location_hint=args.location_hint,
        iterations=args.iterations,
        use_llm=not args.no_llm,
        use_vlm=not args.no_vlm,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_process_audio(args) -> None:
    from .brainlive_daemon_v15_3 import process_audio_file
    print(json.dumps(process_audio_file(args.live_session_id, args.path, speaker_person_id=args.speaker_person_id, speaker_label=args.speaker_label, vad_backend=args.vad_backend, asr_backend=args.asr_backend), ensure_ascii=False, indent=2))


def cmd_brainlive_refresh_hot_context(args) -> None:
    from .brainlive_daemon_v15_3 import refresh_active_context_hot
    print(json.dumps(refresh_active_context_hot(args.live_session_id, trigger_reason=args.reason, limit=args.limit), ensure_ascii=False, indent=2))


def cmd_brainlive_delivery_queue(args) -> None:
    from .db import connect
    from .utils import json_loads
    with connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM brainlive_intervention_delivery_queue WHERE live_session_id=? AND (?='all' OR delivery_status=?) ORDER BY priority DESC, created_at DESC LIMIT ?", (args.live_session_id, args.status, args.status, args.limit))]
    for r in rows:
        r['evidence'] = json_loads(r.pop('evidence_json', '{}'), {})
    print(json.dumps(rows, ensure_ascii=False, indent=2))

def cmd_brainlive_delivery_feedback(args) -> None:
    """Record explicit delivery/display/response evidence for one live intervention."""
    from .v18_8_live_policy import record_delivery_feedback, materialize_intervention_outcome_observation
    evidence = {}
    if getattr(args, "evidence_json", None):
        try:
            parsed = json.loads(args.evidence_json)
        except Exception as exc:
            raise SystemExit(f"--evidence-json must be valid JSON: {exc}")
        if not isinstance(parsed, dict):
            raise SystemExit("--evidence-json must decode to a JSON object")
        evidence = parsed
    feedback = record_delivery_feedback(
        delivery_id=args.delivery_id,
        feedback_type=args.type,
        feedback_source=args.source,
        note=args.note,
        evidence=evidence,
    )
    status = "feedback_explicit" if args.type in {"acted", "dismissed", "ignored"} else "observation_pending"
    outcome = materialize_intervention_outcome_observation(
        delivery_id=args.delivery_id,
        outcome_status=status,
        observed_later_summary=args.note,
        did_help=True if args.type == "acted" else False if args.type in {"dismissed", "ignored"} else None,
        evidence={"feedback_type": args.type, "feedback_source": args.source, **evidence},
    )
    print(json.dumps({"feedback": feedback, "outcome": outcome}, ensure_ascii=False, indent=2))


def cmd_brainlive_drain_hot_retries(args) -> None:
    """Resume durable hot LLM decisions without rebuilding their capsule."""
    from .brainlive_hotloop_v15_6 import drain_due_hot_llm_decisions
    print(json.dumps(
        drain_due_hot_llm_decisions(live_session_id=args.live_session_id, limit=args.limit),
        ensure_ascii=False,
        indent=2,
    ))


def cmd_brainlive_sensor_audit(args) -> None:
    from .brainlive_sensor_fusion_v15_4 import sensor_fusion_audit
    print(json.dumps(sensor_fusion_audit(), ensure_ascii=False, indent=2))


def cmd_brainlive_sensor_config(args) -> None:
    from .brainlive_sensor_fusion_v15_4 import configure_sensor_fusion
    print(json.dumps(configure_sensor_fusion(
        person_id=_require_person_id(args),
        vad_backend=args.vad_backend,
        asr_backend=args.asr_backend,
        speaker_backend=args.speaker_backend,
        vlm_backend=args.vlm_backend,
        sensor_window_s=args.sensor_window,
        context_refresh_s=args.context_refresh,
        proactive_confidence_min=args.proactive_confidence_min,
        proactive_gain_min=args.proactive_gain_min,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_sensor_audio(args) -> None:
    from .brainlive_sensor_fusion_v15_4 import process_audio_sensor
    print(json.dumps(process_audio_sensor(args.live_session_id, args.path, vad_backend=args.vad_backend, asr_backend=args.asr_backend, language=args.language, allow_energy_fallback=not args.no_energy_fallback), ensure_ascii=False, indent=2))


def cmd_brainlive_sensor_image(args) -> None:
    from .brainlive_sensor_fusion_v15_4 import ingest_image_sensor
    print(json.dumps(ingest_image_sensor(args.live_session_id, args.path, model=args.model, timeout=args.timeout, use_vlm=not args.no_vlm), ensure_ascii=False, indent=2))


def cmd_brainlive_sensor_fuse(args) -> None:
    from .brainlive_sensor_fusion_v15_4 import build_fused_situation
    gps = json.loads(args.gps_json) if args.gps_json else None
    print(json.dumps(build_fused_situation(args.live_session_id, explicit_location=args.location_hint, gps_json=gps, force_context_refresh=args.force_context_refresh, use_llm=not args.no_llm), ensure_ascii=False, indent=2))


def cmd_brainlive_sensor_cycle(args) -> None:
    from .brainlive_sensor_fusion_v15_4 import full_sensor_live_cycle
    gps = json.loads(args.gps_json) if args.gps_json else None
    print(json.dumps(full_sensor_live_cycle(
        args.live_session_id,
        audio_path=args.audio,
        text=args.text,
        image_path=args.image,
        explicit_location=args.location_hint,
        gps_json=gps,
        vad_backend=args.vad_backend,
        asr_backend=args.asr_backend,
        use_llm=not args.no_llm,
        use_vlm=not args.no_vlm,
        force_context_refresh=args.force_context_refresh,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_service_start(args) -> None:
    from .brainlive_service_v15_5 import start_brainlive_service
    print(json.dumps(start_brainlive_service(
        person_id=_require_person_id(args),
        service_config_id=args.config_id,
        live_session_id=args.live_session_id,
        title=args.title,
        audio_dir=args.audio_dir,
        transcript_dir=args.transcript_dir,
        image_dir=args.image_dir,
        gps_state_path=args.gps_state_path,
        location_hint=args.location_hint,
        max_iterations=args.max_iterations,
        post_stop_deep_flow=False if args.no_post_stop_deep_flow else None,
        post_stop_use_llm=not args.no_post_stop_llm,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_service_stop(args) -> None:
    from .brainlive_service_v15_5 import stop_brainlive_service
    print(json.dumps(stop_brainlive_service(
        live_session_id=args.live_session_id,
        service_run_id=args.service_run_id,
        close_day=bool(args.close_day),
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_close_day(args) -> None:
    from .v18_close_day import close_brainlive_day
    print(json.dumps(close_brainlive_day(
        person_id=_require_person_id(args),
        live_session_id=args.live_session_id,
        service_run_id=args.service_run_id,
        package_date=args.package_date,
        use_llm=not args.no_llm,
        force=bool(args.force),
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_close_day_status(args) -> None:
    from .v18_close_day import close_day_status
    print(json.dumps(close_day_status(
        person_id=_require_person_id(args),
        package_date=args.package_date,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_service_status(args) -> None:
    from .brainlive_service_v15_5 import brainlive_service_status
    print(json.dumps(brainlive_service_status(live_session_id=args.live_session_id, service_run_id=args.service_run_id), ensure_ascii=False, indent=2))


def cmd_brainlive_service_audit(args) -> None:
    from .brainlive_service_v15_5 import service_audit
    print(json.dumps(service_audit(), ensure_ascii=False, indent=2))


def cmd_brainlive_inbox_status_v158(args) -> None:
    from .brainlive_service_v15_5 import brainlive_inbox_status
    print(json.dumps(brainlive_inbox_status(), ensure_ascii=False, indent=2))


def cmd_brainlive_readiness_audit_v158(args) -> None:
    from .brainlive_readiness_v15_8 import brainlive_brain2_readiness_audit
    print(json.dumps(brainlive_brain2_readiness_audit(person_id=args.person_id), ensure_ascii=False, indent=2))



def cmd_brainlive_personal_model_build_v159(args) -> None:
    from .brainlive_personal_model_v15_9 import build_brain2_live_personal_model
    active_people = json.loads(args.active_people) if args.active_people else None
    print(json.dumps(build_brain2_live_personal_model(
        _require_person_id(args),
        live_session_id=args.live_session_id,
        active_people=active_people,
        place_hint=args.place_hint,
        topic_hint=args.topic_hint,
        use_llm=not args.no_llm,
        timeout=args.timeout,
        limit=args.limit,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_personal_model_audit_v159(args) -> None:
    from .brainlive_personal_model_v15_9 import brainlive_personal_model_audit
    print(json.dumps(brainlive_personal_model_audit(_require_person_id(args)), ensure_ascii=False, indent=2))


def cmd_brainlive_personal_model_latest_v159(args) -> None:
    from .brainlive_personal_model_v15_9 import latest_live_personal_model
    print(json.dumps(latest_live_personal_model(_require_person_id(args), live_session_id=args.live_session_id) or {"status":"missing"}, ensure_ascii=False, indent=2))



def cmd_brain2_life_model_build_v1510(args) -> None:
    from .brain2_life_model_v15_10 import build_brain2_canonical_life_model
    print(json.dumps(build_brain2_canonical_life_model(
        _require_person_id(args),
        period_start=args.period_start,
        period_end=args.period_end,
        use_llm=not args.no_llm,
        timeout=args.timeout,
        limit=args.limit,
    ), ensure_ascii=False, indent=2))


def cmd_brain2_life_model_audit_v1510(args) -> None:
    from .brain2_life_model_v15_10 import brain2_life_model_audit
    print(json.dumps(brain2_life_model_audit(_require_person_id(args)), ensure_ascii=False, indent=2))


def cmd_brain2_life_model_latest_v1510(args) -> None:
    from .brain2_life_model_v15_10 import latest_canonical_life_model
    print(json.dumps(latest_canonical_life_model(_require_person_id(args)) or {"status":"missing"}, ensure_ascii=False, indent=2))


def cmd_brain2_heuristic_audit_v1510(args) -> None:
    from .brain2_life_model_v15_10 import audit_brain2_heuristics
    print(json.dumps(audit_brain2_heuristics(), ensure_ascii=False, indent=2))


def cmd_brain2_life_model_update_v1513(args) -> None:
    from .brain2_life_model_updater_v15_13 import run_brain2_life_model_update
    print(json.dumps(run_brain2_life_model_update(
        _require_person_id(args),
        period_start=args.period_start,
        period_end=args.period_end,
        use_llm=not args.no_llm,
        timeout=args.timeout,
        limit=args.limit,
        bootstrap_if_empty=not args.no_bootstrap,
    ), ensure_ascii=False, indent=2))


def cmd_brain2_life_model_strata_v1513(args) -> None:
    from .brain2_life_model_updater_v15_13 import latest_life_model_strata
    print(json.dumps(latest_life_model_strata(_require_person_id(args)), ensure_ascii=False, indent=2))


def cmd_brain2_life_model_update_audit_v1513(args) -> None:
    from .brain2_life_model_updater_v15_13 import brain2_life_model_update_audit
    print(json.dumps(brain2_life_model_update_audit(_require_person_id(args)), ensure_ascii=False, indent=2))




def cmd_brain2_longitudinal_run_v17(args) -> None:
    from .brain2_longitudinal_cases_v17 import run_longitudinal_consolidation
    print(json.dumps(run_longitudinal_consolidation(
        person_id=_require_person_id(args),
        period=args.period,
        run_date=args.run_date,
        period_start=args.period_start,
        period_end=args.period_end,
        use_llm=not args.no_llm,
        run_periodic_mirror_layer=not args.no_periodic_mirror,
        force_cases=args.force_cases,
    ), ensure_ascii=False, indent=2))


def cmd_brain2_longitudinal_digest_v17(args) -> None:
    from .brain2_longitudinal_cases_v17 import longitudinal_memory_digest
    print(json.dumps(longitudinal_memory_digest(_require_person_id(args), limit=args.limit), ensure_ascii=False, indent=2))

def cmd_brainlive_event_assemble_v1514(args) -> None:
    from .brainlive_event_assembler_v15_14 import run_brainlive_event_assembly
    print(json.dumps(run_brainlive_event_assembly(
        _require_person_id(args),
        package_date=args.package_date,
        export_to_brain2=not args.no_export,
        limit_per_table=args.limit_per_table,
        gap_minutes=args.gap_minutes,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_event_assembly_audit_v1514(args) -> None:
    from .brainlive_event_assembler_v15_14 import event_assembly_audit
    print(json.dumps(event_assembly_audit(_require_person_id(args), package_date=args.package_date), ensure_ascii=False, indent=2))



def cmd_brainlive_silent_life_mine_v160(args) -> None:
    from .brainlive_silent_life_v16_0 import mine_silent_nonverbal_life_events
    print(json.dumps(mine_silent_nonverbal_life_events(
        _require_person_id(args),
        package_date=args.package_date,
        use_llm=not args.no_llm,
        timeout=args.timeout,
        transcript_char_threshold=args.transcript_char_threshold,
        export_life_events=not args.no_export,
        limit=args.limit,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_silent_life_audit_v160(args) -> None:
    from .brainlive_silent_life_v16_0 import silent_life_audit
    print(json.dumps(silent_life_audit(_require_person_id(args), package_date=args.package_date), ensure_ascii=False, indent=2))


def cmd_brainlive_deep_vision_run_v161(args) -> None:
    from .brainlive_offline_deep_vision_v16_1 import run_offline_deep_vision_for_bundles
    print(json.dumps(run_offline_deep_vision_for_bundles(
        _require_person_id(args),
        package_date=args.package_date,
        model=args.model,
        timeout_per_image=args.timeout_per_image,
        max_keyframes_per_bundle=args.max_keyframes_per_bundle,
        transcript_char_threshold=args.transcript_char_threshold,
        limit_bundles=args.limit_bundles,
        append_to_brain2=not args.no_append_to_brain2,
        fail_on_vlm_error=args.fail_on_vlm_error,
        use_vlm=not args.no_vlm,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_deep_vision_audit_v161(args) -> None:
    from .brainlive_offline_deep_vision_v16_1 import deep_vision_audit
    print(json.dumps(deep_vision_audit(_require_person_id(args), package_date=args.package_date), ensure_ascii=False, indent=2))

def cmd_brainlive_deep_audio_run_v185(args) -> None:
    from .brainlive_offline_deep_audio_v18_5 import run_offline_deep_audio_for_bundles
    print(json.dumps(run_offline_deep_audio_for_bundles(
        person_id=_require_person_id(args),
        package_date=args.package_date,
        live_session_id=args.live_session_id,
        language=args.language,
        max_bundle_audio_seconds=args.max_bundle_audio_seconds,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_deep_audio_audit_v185(args) -> None:
    from .brainlive_offline_deep_audio_v18_5 import deep_audio_audit
    print(json.dumps(deep_audio_audit(_require_person_id(args), package_date=args.package_date), ensure_ascii=False, indent=2))


def cmd_brainlive_post_stop_flow_v1515(args) -> None:
    from .brainlive_poststop_deep_flow_v15_15 import run_brainlive_post_stop_deep_flow
    print(json.dumps(run_brainlive_post_stop_deep_flow(
        person_id=_require_person_id(args),
        live_session_id=args.live_session_id,
        service_run_id=args.service_run_id,
        package_date=args.package_date,
        limit_per_table=args.limit_per_table,
        gap_minutes=args.gap_minutes,
        force=args.force,
        run_brain2=not args.no_brain2,
        run_v15=not args.no_v15,
        use_llm=not args.no_llm,
        run_silent_life=not args.no_silent_life,
        silent_life_timeout=args.silent_life_timeout,
        run_deep_audio=not args.no_deep_audio,
        deep_audio_language=args.deep_audio_language,
        deep_audio_max_bundle_seconds=args.deep_audio_max_bundle_seconds,
        run_deep_vision=not args.no_deep_vision,
        deep_vision_model=args.deep_vision_model,
        deep_vision_timeout_per_image=args.deep_vision_timeout_per_image,
        deep_vision_max_keyframes_per_bundle=args.deep_vision_max_keyframes_per_bundle,
    ), ensure_ascii=False, indent=2))


def cmd_brainlive_post_stop_flow_audit_v1515(args) -> None:
    from .brainlive_poststop_deep_flow_v15_15 import post_stop_deep_flow_audit
    print(json.dumps(post_stop_deep_flow_audit(_require_person_id(args), package_date=args.package_date), ensure_ascii=False, indent=2))

def main(argv: list[str] | None = None) -> None:
    parser=argparse.ArgumentParser(prog="mlomega-audio", description="MemoryLight Omega Audio Elite V14.5 Brain 2.0 People + Open Loops Final")
    sub=parser.add_subparsers(required=True)
    p=sub.add_parser("init-db"); p.set_defaults(func=cmd_init_db)
    p=sub.add_parser("seed-example"); p.set_defaults(func=cmd_seed_example)
    p=sub.add_parser("ingest-transcript"); p.add_argument("path"); p.set_defaults(func=cmd_ingest_transcript)
    p=sub.add_parser("ingest-audio"); p.add_argument("path"); p.add_argument("--language", default="fr"); p.add_argument("--speaker-map"); p.set_defaults(func=cmd_ingest_audio)
    p=sub.add_parser("query"); p.add_argument("question"); p.set_defaults(func=cmd_query)
    p=sub.add_parser("consolidate"); p.set_defaults(func=cmd_consolidate)
    p=sub.add_parser("sync-vectors"); p.add_argument("--person-id", required=True); p.add_argument("--limit", type=int); p.add_argument("--conversation-id"); p.add_argument("--full", action="store_true"); p.set_defaults(func=cmd_sync_vectors)
    p=sub.add_parser("sync-external"); p.add_argument("conversation_id"); p.add_argument("--person-id", required=True); p.set_defaults(func=cmd_sync_external)
    p=sub.add_parser("graph"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_graph)
    p=sub.add_parser("timeline"); p.add_argument("--topic"); p.set_defaults(func=cmd_timeline)
    p=sub.add_parser("speakers"); p.set_defaults(func=cmd_speakers)
    p=sub.add_parser("memory-overview"); p.set_defaults(func=cmd_memory_overview)
    p=sub.add_parser("memory-card"); p.add_argument("id"); p.set_defaults(func=cmd_memory_card)
    p=sub.add_parser("memory-revise"); p.add_argument("table"); p.add_argument("id"); p.add_argument("--person-id", required=True); p.add_argument("--type", default="correct"); p.add_argument("--reason", required=True); p.add_argument("--patch", help="JSON des champs corrigés"); p.add_argument("--confidence", type=float, default=1.0); p.set_defaults(func=cmd_memory_revise)
    p=sub.add_parser("sync-jobs"); p.add_argument("--status"); p.add_argument("--backend"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_sync_jobs)
    p=sub.add_parser("sync-pending"); p.add_argument("--backend"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_sync_pending)
    p=sub.add_parser("mem0-config"); p.set_defaults(func=cmd_mem0_config)
    p=sub.add_parser("mem0-doctor"); p.add_argument("--fail", action="store_true"); p.add_argument("--show-config", action="store_true"); p.set_defaults(func=cmd_mem0_doctor)
    p=sub.add_parser("enroll-voice"); p.add_argument("person_id"); p.add_argument("path"); p.add_argument("--display-name"); p.add_argument("--is-user", action="store_true"); p.set_defaults(func=cmd_enroll_voice)
    p=sub.add_parser("match-voice"); p.add_argument("path"); p.set_defaults(func=cmd_match_voice)
    p=sub.add_parser("setup-me"); p.add_argument("path"); p.add_argument("--display-name", default="Moi / Will"); p.add_argument("--person-id", default="me"); p.set_defaults(func=cmd_setup_me)
    p=sub.add_parser("voice-pending"); p.set_defaults(func=cmd_voice_pending)
    p=sub.add_parser("name-voice"); p.add_argument("cluster_id"); p.add_argument("person_id"); p.add_argument("--display-name"); p.add_argument("--is-user", action="store_true"); p.set_defaults(func=cmd_name_voice)
    p=sub.add_parser("preprocess-audio"); p.add_argument("path"); p.add_argument("--keep-silence", action="store_true"); p.add_argument("--remove-silence", action="store_true", help="Unsafe unless you accept non-exact original timestamps; flow-watch does not use it."); p.add_argument("--max-chunk-seconds", type=int, default=900); p.add_argument("--silence-threshold-db", default="-40dB"); p.add_argument("--min-silence-seconds", type=float, default=1.2); p.set_defaults(func=cmd_preprocess_audio)
    p=sub.add_parser("flow-once"); p.add_argument("path"); p.add_argument("--no-v13", action="store_true"); p.add_argument("--no-preprocess", action="store_true"); p.add_argument("--max-chunk-seconds", type=int, default=900); p.set_defaults(func=cmd_flow_once)
    p=sub.add_parser("flow-watch"); p.add_argument("--audio-dir"); p.add_argument("--transcript-dir"); p.add_argument("--poll-seconds", type=float, default=30.0); p.add_argument("--once", action="store_true"); p.add_argument("--no-v13", action="store_true"); p.set_defaults(func=cmd_flow_watch)
    p=sub.add_parser("v12-build"); p.add_argument("conversation_id", nargs="?"); p.set_defaults(func=cmd_v12_build)
    p=sub.add_parser("v12-overview"); p.set_defaults(func=cmd_v12_overview)
    p=sub.add_parser("v12-predict"); p.add_argument("target", choices=sorted(__import__("mlomega_audio_elite.behavior_v12", fromlist=["PREDICTION_TARGETS"]).PREDICTION_TARGETS)); p.add_argument("context"); p.add_argument("--person-id"); p.add_argument("--horizon", default="next"); p.set_defaults(func=cmd_v12_predict)
    p=sub.add_parser("v12-verify"); p.add_argument("prediction_id"); p.add_argument("observed"); p.add_argument("--match-score", type=float); p.add_argument("--note"); p.set_defaults(func=cmd_v12_verify)
    p=sub.add_parser("v13-audit-plan"); p.set_defaults(func=cmd_v13_audit_plan)
    p=sub.add_parser("v13-build"); p.add_argument("conversation_id", nargs="?"); p.add_argument("--max-episodes", type=int); p.set_defaults(func=cmd_v13_build)
    p=sub.add_parser("v13-overview"); p.set_defaults(func=cmd_v13_overview)
    p=sub.add_parser("v13-predict"); p.add_argument("target", choices=sorted(__import__("mlomega_audio_elite.behavior_v13", fromlist=["V13_TARGETS"]).V13_TARGETS)); p.add_argument("context"); p.add_argument("--person-id"); p.add_argument("--horizon", default="next"); p.set_defaults(func=cmd_v13_predict)
    p=sub.add_parser("v13-verify"); p.add_argument("prediction_id"); p.add_argument("observed"); p.add_argument("--match-score", type=float); p.add_argument("--note"); p.set_defaults(func=cmd_v13_verify)
    p=sub.add_parser("v13-subtopics"); p.add_argument("conversation_id"); p.set_defaults(func=cmd_v13_subtopics)
    p=sub.add_parser("v13-discover-outcomes"); p.add_argument("conversation_id"); p.add_argument("--limit-pending", type=int, default=80); p.set_defaults(func=cmd_v13_discover_outcomes)
    p=sub.add_parser("v13-autonomous"); p.add_argument("conversation_id"); p.set_defaults(func=cmd_v13_autonomous)
    p=sub.add_parser("v13-insights"); p.add_argument("--status", default="open"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_v13_insights)
    p=sub.add_parser("v13-ask"); p.add_argument("question"); p.add_argument("--person-id"); p.set_defaults(func=cmd_v13_ask)
    p=sub.add_parser("v14-audit"); p.set_defaults(func=cmd_v14_audit)
    p=sub.add_parser("v14-mirror"); p.add_argument("conversation_id", nargs="?"); p.add_argument("--person-id", required=True); p.add_argument("--scope", default="long_horizon"); p.set_defaults(func=cmd_v14_mirror)
    p=sub.add_parser("v14-insights"); p.add_argument("--person-id"); p.add_argument("--status", default="open"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_v14_insights)
    p=sub.add_parser("v14-digest"); p.add_argument("--person-id"); p.add_argument("--limit", type=int, default=10); p.set_defaults(func=cmd_v14_digest)
    p=sub.add_parser("v14-ask"); p.add_argument("question"); p.add_argument("--person-id"); p.set_defaults(func=cmd_v14_ask)
    p=sub.add_parser("v14-route"); p.add_argument("question"); p.add_argument("--person-id"); p.set_defaults(func=cmd_v14_route)
    p=sub.add_parser("v14-select"); p.add_argument("question"); p.add_argument("--person-id"); p.add_argument("--limit", type=int, default=80); p.set_defaults(func=cmd_v14_select)
    p=sub.add_parser("v14-1-audit"); p.set_defaults(func=cmd_v14_1_audit)
    p=sub.add_parser("v14-2-audit"); p.set_defaults(func=cmd_v14_2_audit)
    p=sub.add_parser("v14-3-audit"); p.set_defaults(func=cmd_v14_3_audit)
    p=sub.add_parser("v14-4-audit"); p.set_defaults(func=cmd_v14_4_audit)
    p=sub.add_parser("v14-5-audit"); p.set_defaults(func=cmd_v14_5_audit)
    p=sub.add_parser("v14-6-audit"); p.set_defaults(func=cmd_v14_6_audit)
    p=sub.add_parser("v14-7-audit"); p.set_defaults(func=cmd_v14_7_audit)
    p=sub.add_parser("v14-8-audit"); p.set_defaults(func=cmd_v14_8_audit)
    p=sub.add_parser("v14-auto-verify"); p.add_argument("--conversation-id"); p.add_argument("--person-id"); p.add_argument("--limit", type=int, default=50); p.add_argument("--min-confidence", type=float, default=0.55); p.add_argument("--allow-existing-results", action="store_true"); p.set_defaults(func=cmd_v14_auto_verify)
    p=sub.add_parser("v14-5-run"); p.add_argument("conversation_id"); p.add_argument("--person-id"); p.set_defaults(func=cmd_v14_5_run)
    p=sub.add_parser("v14-people-hypotheses"); p.add_argument("--status", default="pending_confirmation"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_v14_people_hypotheses)
    p=sub.add_parser("v14-open-loops"); p.add_argument("--person-id"); p.add_argument("--status"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_v14_open_loops)
    p=sub.add_parser("v14-6-run"); p.add_argument("conversation_id"); p.add_argument("--person-id"); p.set_defaults(func=cmd_v14_6_run)
    p=sub.add_parser("v14-people-models"); p.add_argument("--person-id"); p.add_argument("--person-hint"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_v14_people_models)
    p=sub.add_parser("v14-social-aftereffects"); p.add_argument("--person-id"); p.add_argument("--status", default="open"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_v14_social_aftereffects)
    p=sub.add_parser("v14-proactive-run"); p.add_argument("conversation_id", nargs="?"); p.add_argument("--person-id"); p.add_argument("--trigger-type", default="manual"); p.add_argument("--limit", type=int, default=30); p.set_defaults(func=cmd_v14_proactive_run)
    p=sub.add_parser("v14-interventions"); p.add_argument("--person-id", required=True); p.add_argument("--status"); p.add_argument("--priority"); p.add_argument("--limit", type=int, default=30); p.set_defaults(func=cmd_v14_interventions)
    p=sub.add_parser("v14-intervention-feedback"); p.add_argument("queue_id"); p.add_argument("--person-id"); p.add_argument("--type", default="dismissed", choices=["dismissed","acted","helpful","not_relevant","too_intrusive","snoozed","delivered"]); p.add_argument("--note"); p.add_argument("--helpfulness", type=float); p.add_argument("--action-taken"); p.set_defaults(func=cmd_v14_intervention_feedback)
    p=sub.add_parser("v14-intervention-export"); p.add_argument("--person-id"); p.add_argument("--output-dir"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_v14_intervention_export)
    p=sub.add_parser("v14-intervention-policy"); p.add_argument("--person-id"); p.add_argument("--patch", help="JSON patch, ex: {\"min_notify_confidence\":0.7}"); p.set_defaults(func=cmd_v14_intervention_policy)
    p=sub.add_parser("v14-clarification-run"); p.add_argument("conversation_id", nargs="?"); p.add_argument("--person-id"); p.add_argument("--trigger-type", default="manual"); p.add_argument("--limit", type=int, default=80); p.set_defaults(func=cmd_v14_clarification_run)
    p=sub.add_parser("v14-clarifications"); p.add_argument("--person-id"); p.add_argument("--status", default="queued", choices=["queued","watching","answered","needs_followup","dismissed","all"]); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_v14_clarifications)
    p=sub.add_parser("v14-answer"); p.add_argument("item_id"); p.add_argument("answer"); p.add_argument("--person-id", required=True); p.set_defaults(func=cmd_v14_answer)
    p=sub.add_parser("v14-clarification-export"); p.add_argument("--person-id"); p.add_argument("--output-dir"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_v14_clarification_export)
    p=sub.add_parser("v14-clarification-policy"); p.add_argument("--person-id"); p.add_argument("--patch", help="JSON patch, ex: {\"max_new_questions_per_run\":2}"); p.set_defaults(func=cmd_v14_clarification_policy)
    p=sub.add_parser("v14-autopilot-coverage"); p.set_defaults(func=cmd_v14_autopilot_coverage)
    p=sub.add_parser("v14-auto-consolidate"); p.add_argument("--person-id"); p.add_argument("--periods", default="hour,day,week,month"); p.add_argument("--force", action="store_true"); p.add_argument("--no-export", action="store_true"); p.set_defaults(func=cmd_v14_auto_consolidate)
    p=sub.add_parser("v14-scheduler-status"); p.add_argument("--person-id"); p.set_defaults(func=cmd_v14_scheduler_status)
    p=sub.add_parser("export-self-model"); p.add_argument("--person-id"); p.add_argument("--format", choices=["markdown","json"], default="markdown"); p.add_argument("--scope", default="full"); p.add_argument("--output-dir"); p.add_argument("--limit", type=int, default=80); p.set_defaults(func=cmd_export_self_model)
    p=sub.add_parser("v14-self-model"); p.add_argument("--person-id"); p.add_argument("--limit", type=int, default=80); p.set_defaults(func=cmd_v14_self_model)
    p=sub.add_parser("v14-consolidate"); p.add_argument("--person-id"); p.add_argument("--period", choices=["hour","day","week","month","quarter","year","all_time"], default="day"); p.add_argument("--start"); p.add_argument("--end"); p.set_defaults(func=cmd_v14_consolidate)
    p=sub.add_parser("v14-today"); p.add_argument("--person-id"); p.set_defaults(func=cmd_v14_today)
    p=sub.add_parser("v14-snapshots"); p.add_argument("--person-id"); p.add_argument("--limit", type=int, default=10); p.set_defaults(func=cmd_v14_snapshots)

    p=sub.add_parser("brainlive-audit"); p.set_defaults(func=cmd_brainlive_audit)
    p=sub.add_parser("brainlive-start"); p.add_argument("--person-id"); p.add_argument("--title"); p.add_argument("--active-people", help="JSON list"); p.add_argument("--location-hint"); p.add_argument("--mode", default="unknown"); p.set_defaults(func=cmd_brainlive_start)
    p=sub.add_parser("brainlive-end"); p.add_argument("live_session_id"); p.add_argument("--notes"); p.set_defaults(func=cmd_brainlive_end)
    p=sub.add_parser("brainlive-turn"); p.add_argument("live_session_id"); p.add_argument("text"); p.add_argument("--speaker-label"); p.add_argument("--speaker-person-id"); p.add_argument("--speaker-confidence", type=float, default=0.0); p.add_argument("--partial", action="store_true"); p.add_argument("--timestamp-start"); p.add_argument("--timestamp-end"); p.add_argument("--metadata", help="JSON object"); p.set_defaults(func=cmd_brainlive_turn)
    p=sub.add_parser("brainlive-context"); p.add_argument("live_session_id"); p.add_argument("--active-people", help="JSON list"); p.add_argument("--limit", type=int, default=20); p.add_argument("--full", action="store_true"); p.set_defaults(func=cmd_brainlive_context)
    p=sub.add_parser("brainlive-run"); p.add_argument("live_session_id"); p.add_argument("--mode", default="deep_live"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--timeout", type=float, default=480.0); p.add_argument("--active-people", help="JSON list"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_brainlive_run)
    p=sub.add_parser("brainlive-inbox"); p.add_argument("--person-id"); p.add_argument("--status", default="all"); p.add_argument("--limit", type=int, default=30); p.set_defaults(func=cmd_brainlive_inbox)
    p=sub.add_parser("brainlive-outcome"); p.add_argument("forecast_id", nargs="?"); p.add_argument("observed_after"); p.add_argument("--person-id"); p.add_argument("--live-session-id"); p.add_argument("--candidate-id"); p.add_argument("--match-score", type=float); p.add_argument("--user-feedback"); p.set_defaults(func=cmd_brainlive_outcome)
    p=sub.add_parser("brainlive-disagree"); p.add_argument("system_claim"); p.add_argument("user_response"); p.add_argument("--candidate-id"); p.add_argument("--person-id"); p.add_argument("--live-session-id"); p.set_defaults(func=cmd_brainlive_disagree)
    p=sub.add_parser("brainlive-nightly"); p.add_argument("--person-id"); p.add_argument("--run-date"); p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_brainlive_nightly)
    p=sub.add_parser("vision-ingest-frame"); p.add_argument("path"); p.add_argument("--live-session-id"); p.add_argument("--conversation-id"); p.add_argument("--captured-at"); p.add_argument("--device-source"); p.add_argument("--observation-json"); p.add_argument("--observation-file"); p.add_argument("--model", default="manual_or_external_vlm"); p.set_defaults(func=cmd_vision_ingest_frame)

    p=sub.add_parser("brainlive-longitudinal-audit"); p.set_defaults(func=cmd_brainlive_longitudinal_audit)
    p=sub.add_parser("brainlive-mine-routines"); p.add_argument("--person-id"); p.add_argument("--start"); p.add_argument("--end"); p.add_argument("--min-support", type=int, default=3); p.add_argument("--no-llm", action="store_true"); p.add_argument("--timeout", type=float, default=480.0); p.set_defaults(func=cmd_brainlive_mine_routines)
    p=sub.add_parser("brainlive-hypotheses-run"); p.add_argument("--person-id"); p.add_argument("--routine-id", action="append"); p.add_argument("--timeout", type=float, default=480.0); p.set_defaults(func=cmd_brainlive_hypotheses_run)
    p=sub.add_parser("brainlive-outcomes-auto"); p.add_argument("--person-id"); p.add_argument("--limit", type=int, default=30); p.add_argument("--timeout", type=float, default=480.0); p.set_defaults(func=cmd_brainlive_outcomes_auto)
    p=sub.add_parser("brainlive-interpret-disagreement"); p.add_argument("disagreement_id"); p.add_argument("--timeout", type=float, default=480.0); p.set_defaults(func=cmd_brainlive_interpret_disagreement)
    p=sub.add_parser("brainlive-match-affordances"); p.add_argument("live_session_id"); p.add_argument("--timeout", type=float, default=480.0); p.set_defaults(func=cmd_brainlive_match_affordances)
    p=sub.add_parser("brainlive-scheduler-config"); p.add_argument("--person-id"); p.add_argument("--timezone", default="Europe/Paris"); p.add_argument("--daytime-tick-minutes", type=int, default=5); p.add_argument("--nightly-time", default="03:30"); p.add_argument("--brain2-periods", default="day"); p.set_defaults(func=cmd_brainlive_scheduler_config)
    p=sub.add_parser("brainlive-scheduler-tick"); p.add_argument("--person-id"); p.add_argument("--kind", choices=["daytime","nightly"], default="daytime"); p.add_argument("--live-session-id"); p.add_argument("--run-date"); p.add_argument("--timeout", type=float, default=480.0); p.set_defaults(func=cmd_brainlive_scheduler_tick)
    p=sub.add_parser("brainlive-replay"); p.add_argument("--person-id"); p.add_argument("--conversation-id"); p.add_argument("--start"); p.add_argument("--end"); p.add_argument("--step-turns", type=int, default=8); p.add_argument("--timeout", type=float, default=480.0); p.set_defaults(func=cmd_brainlive_replay)

    p=sub.add_parser("brainlive-realtime-audit"); p.set_defaults(func=cmd_brainlive_realtime_audit)
    p=sub.add_parser("brainlive-runtime-profile"); p.add_argument("--person-id", required=True); p.add_argument("--h0-timeout", type=float, default=2.0); p.add_argument("--h1-timeout", type=float, default=5.0); p.add_argument("--h2-timeout", type=float, default=12.0); p.add_argument("--vlm-timeout", type=float, default=8.0); p.set_defaults(func=cmd_brainlive_runtime_profile)
    p=sub.add_parser("brainlive-live-image"); p.add_argument("live_session_id"); p.add_argument("path"); p.add_argument("--device-source"); p.add_argument("--model"); p.add_argument("--timeout", type=float); p.add_argument("--no-vlm", action="store_true"); p.set_defaults(func=cmd_brainlive_live_image)
    p=sub.add_parser("brainlive-live-tick"); p.add_argument("live_session_id"); p.add_argument("--horizon", choices=["H0","H1","H2"], default="H1"); p.add_argument("--text"); p.add_argument("--image"); p.add_argument("--audio-sample"); p.add_argument("--speaker-label"); p.add_argument("--speaker-person-id"); p.add_argument("--speaker-confidence", type=float); p.add_argument("--location-hint"); p.add_argument("--timeout", type=float); p.add_argument("--no-vlm", action="store_true"); p.add_argument("--no-llm", action="store_true"); p.set_defaults(func=cmd_brainlive_live_tick)
    p=sub.add_parser("brainlive-live-cycle"); p.add_argument("live_session_id"); p.add_argument("--text"); p.add_argument("--image"); p.add_argument("--audio-sample"); p.add_argument("--speaker-label"); p.add_argument("--speaker-person-id"); p.add_argument("--location-hint"); p.add_argument("--no-vlm", action="store_true"); p.add_argument("--no-llm", action="store_true"); p.set_defaults(func=cmd_brainlive_live_cycle)

    p=sub.add_parser("brainlive-daemon-audit"); p.set_defaults(func=cmd_brainlive_daemon_audit)
    p=sub.add_parser("brainlive-daemon-config"); p.add_argument("--person-id", required=True); p.add_argument("--transcript-dir"); p.add_argument("--image-dir"); p.add_argument("--audio-dir"); p.add_argument("--vad-backend", default="energy"); p.add_argument("--asr-backend", default="external_or_whispercpp"); p.add_argument("--h1-interval", type=float, default=5.0); p.add_argument("--h2-interval", type=float, default=12.0); p.add_argument("--vision-interval", type=float, default=10.0); p.add_argument("--context-refresh", type=float, default=15.0); p.add_argument("--outcome-interval", type=float, default=60.0); p.add_argument("--sleep", type=float, default=1.0); p.add_argument("--max-iterations", type=int, default=0); p.set_defaults(func=cmd_brainlive_daemon_config)
    p=sub.add_parser("brainlive-daemon-iteration"); p.add_argument("live_session_id"); p.add_argument("--config-id"); p.add_argument("--person-id", required=True); p.add_argument("--no-llm", action="store_true"); p.add_argument("--no-vlm", action="store_true"); p.set_defaults(func=cmd_brainlive_daemon_iteration)
    p=sub.add_parser("brainlive-daemon-run"); p.add_argument("--live-session-id"); p.add_argument("--person-id", required=True); p.add_argument("--config-id"); p.add_argument("--title"); p.add_argument("--location-hint"); p.add_argument("--iterations", type=int); p.add_argument("--no-llm", action="store_true"); p.add_argument("--no-vlm", action="store_true"); p.set_defaults(func=cmd_brainlive_daemon_run)
    p=sub.add_parser("brainlive-process-audio"); p.add_argument("live_session_id"); p.add_argument("path"); p.add_argument("--speaker-person-id"); p.add_argument("--speaker-label"); p.add_argument("--vad-backend", default="energy"); p.add_argument("--asr-backend", default="external_or_whispercpp"); p.set_defaults(func=cmd_brainlive_process_audio)
    p=sub.add_parser("brainlive-refresh-hot-context"); p.add_argument("live_session_id"); p.add_argument("--reason", default="manual"); p.add_argument("--limit", type=int, default=32); p.set_defaults(func=cmd_brainlive_refresh_hot_context)
    p=sub.add_parser("brainlive-delivery-queue"); p.add_argument("live_session_id"); p.add_argument("--status", default="queued"); p.add_argument("--limit", type=int, default=30); p.set_defaults(func=cmd_brainlive_delivery_queue)
    p=sub.add_parser("brainlive-delivery-feedback", help="Record delivered/displayed/seen/acted/dismissed/ignored feedback for a live intervention."); p.add_argument("delivery_id"); p.add_argument("--type", required=True, choices=["delivered","displayed","seen","acted","dismissed","ignored","failed"]); p.add_argument("--source", default="cli"); p.add_argument("--note"); p.add_argument("--evidence-json"); p.set_defaults(func=cmd_brainlive_delivery_feedback)

    p=sub.add_parser("brainlive-drain-hot-retries"); p.add_argument("--live-session-id"); p.add_argument("--limit", type=int, default=4); p.set_defaults(func=cmd_brainlive_drain_hot_retries)


    p=sub.add_parser("brainlive-sensor-audit"); p.set_defaults(func=cmd_brainlive_sensor_audit)
    p=sub.add_parser("brainlive-sensor-config"); p.add_argument("--person-id", required=True); p.add_argument("--vad-backend", default="silero"); p.add_argument("--asr-backend", default="faster_or_whispercpp"); p.add_argument("--speaker-backend", default="speechbrain_ecapa"); p.add_argument("--vlm-backend", default="ollama_multimodal"); p.add_argument("--sensor-window", type=float, default=18.0); p.add_argument("--context-refresh", type=float, default=12.0); p.add_argument("--proactive-confidence-min", type=float, default=0.62); p.add_argument("--proactive-gain-min", type=float, default=0.45); p.set_defaults(func=cmd_brainlive_sensor_config)
    p=sub.add_parser("brainlive-sensor-audio"); p.add_argument("live_session_id"); p.add_argument("path"); p.add_argument("--vad-backend"); p.add_argument("--asr-backend"); p.add_argument("--language", default="fr"); p.add_argument("--no-energy-fallback", action="store_true"); p.set_defaults(func=cmd_brainlive_sensor_audio)
    p=sub.add_parser("brainlive-sensor-image"); p.add_argument("live_session_id"); p.add_argument("path"); p.add_argument("--model"); p.add_argument("--timeout", type=float, default=8.0); p.add_argument("--no-vlm", action="store_true"); p.set_defaults(func=cmd_brainlive_sensor_image)
    p=sub.add_parser("brainlive-sensor-fuse"); p.add_argument("live_session_id"); p.add_argument("--location-hint"); p.add_argument("--gps-json"); p.add_argument("--force-context-refresh", action="store_true"); p.add_argument("--no-llm", action="store_true"); p.set_defaults(func=cmd_brainlive_sensor_fuse)
    p=sub.add_parser("brainlive-sensor-cycle"); p.add_argument("live_session_id"); p.add_argument("--audio"); p.add_argument("--text"); p.add_argument("--image"); p.add_argument("--location-hint"); p.add_argument("--gps-json"); p.add_argument("--vad-backend"); p.add_argument("--asr-backend"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--no-vlm", action="store_true"); p.add_argument("--force-context-refresh", action="store_true"); p.set_defaults(func=cmd_brainlive_sensor_cycle)


    p=sub.add_parser("brainlive-start-service"); p.add_argument("--person-id", required=True); p.add_argument("--config-id"); p.add_argument("--live-session-id"); p.add_argument("--title"); p.add_argument("--audio-dir"); p.add_argument("--transcript-dir"); p.add_argument("--image-dir"); p.add_argument("--gps-state-path"); p.add_argument("--location-hint"); p.add_argument("--max-iterations", type=int, default=0); p.add_argument("--no-post-stop-deep-flow", action="store_true"); p.add_argument("--no-post-stop-llm", action="store_true"); p.set_defaults(func=cmd_brainlive_service_start)
    p=sub.add_parser("brainlive-stop-service"); p.add_argument("--live-session-id"); p.add_argument("--service-run-id"); p.add_argument("--close-day", action="store_true", help="After the normal post-stop flow, run the gated longitudinal, Life Model and cleanup-close-day sequence."); p.set_defaults(func=cmd_brainlive_service_stop)
    p=sub.add_parser("brainlive-close-day"); p.add_argument("--person-id", required=True); p.add_argument("--live-session-id"); p.add_argument("--service-run-id"); p.add_argument("--package-date"); p.add_argument("--force", action="store_true"); p.add_argument("--no-llm", action="store_true"); p.set_defaults(func=cmd_brainlive_close_day)
    p=sub.add_parser("brainlive-close-day-status"); p.add_argument("--person-id", required=True); p.add_argument("--package-date"); p.set_defaults(func=cmd_brainlive_close_day_status)
    p=sub.add_parser("brainlive-status"); p.add_argument("--live-session-id"); p.add_argument("--service-run-id"); p.set_defaults(func=cmd_brainlive_service_status)
    p=sub.add_parser("brainlive-service-audit"); p.set_defaults(func=cmd_brainlive_service_audit)

    p=sub.add_parser("brainlive-inbox-status"); p.set_defaults(func=cmd_brainlive_inbox_status_v158)
    p=sub.add_parser("brainlive-readiness-audit"); p.add_argument("--person-id"); p.set_defaults(func=cmd_brainlive_readiness_audit_v158)


    p=sub.add_parser("brainlive-personal-model-build"); p.add_argument("--person-id"); p.add_argument("--live-session-id"); p.add_argument("--active-people", help="JSON list"); p.add_argument("--place-hint"); p.add_argument("--topic-hint"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--timeout", type=float, default=90.0); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_brainlive_personal_model_build_v159)
    p=sub.add_parser("brainlive-personal-model-audit"); p.add_argument("--person-id"); p.set_defaults(func=cmd_brainlive_personal_model_audit_v159)
    p=sub.add_parser("brainlive-personal-model-latest"); p.add_argument("--person-id"); p.add_argument("--live-session-id"); p.set_defaults(func=cmd_brainlive_personal_model_latest_v159)

    p=sub.add_parser("brain2-life-model-build"); p.add_argument("--person-id"); p.add_argument("--period-start"); p.add_argument("--period-end"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--timeout", type=float, default=180.0); p.add_argument("--limit", type=int, default=120); p.set_defaults(func=cmd_brain2_life_model_build_v1510)
    p=sub.add_parser("brain2-life-model-audit"); p.add_argument("--person-id"); p.set_defaults(func=cmd_brain2_life_model_audit_v1510)
    p=sub.add_parser("brain2-life-model-latest"); p.add_argument("--person-id"); p.set_defaults(func=cmd_brain2_life_model_latest_v1510)
    p=sub.add_parser("brain2-heuristic-audit"); p.set_defaults(func=cmd_brain2_heuristic_audit_v1510)
    p=sub.add_parser("brain2-life-model-update"); p.add_argument("--person-id"); p.add_argument("--period-start"); p.add_argument("--period-end"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--no-bootstrap", action="store_true"); p.add_argument("--timeout", type=float, default=180.0); p.add_argument("--limit", type=int, default=120); p.set_defaults(func=cmd_brain2_life_model_update_v1513)
    p=sub.add_parser("brain2-life-model-strata"); p.add_argument("--person-id"); p.set_defaults(func=cmd_brain2_life_model_strata_v1513)
    p=sub.add_parser("brain2-life-model-update-audit"); p.add_argument("--person-id"); p.set_defaults(func=cmd_brain2_life_model_update_audit_v1513)
    p=sub.add_parser("brain2-longitudinal-run"); p.add_argument("--person-id"); p.add_argument("--period", choices=["hour","day","week","month","quarter","year","all_time"], default="day"); p.add_argument("--run-date"); p.add_argument("--period-start"); p.add_argument("--period-end"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--no-periodic-mirror", action="store_true"); p.add_argument("--force-cases", action="store_true"); p.set_defaults(func=cmd_brain2_longitudinal_run_v17)
    p=sub.add_parser("brain2-longitudinal-digest"); p.add_argument("--person-id"); p.add_argument("--limit", type=int, default=30); p.set_defaults(func=cmd_brain2_longitudinal_digest_v17)
    p=sub.add_parser("brainlive-event-assemble"); p.add_argument("--person-id"); p.add_argument("--package-date"); p.add_argument("--no-export", action="store_true"); p.add_argument("--limit-per-table", type=int, default=5000); p.add_argument("--gap-minutes", type=int, default=20); p.set_defaults(func=cmd_brainlive_event_assemble_v1514)
    p=sub.add_parser("brainlive-event-assembly-audit"); p.add_argument("--person-id"); p.add_argument("--package-date"); p.set_defaults(func=cmd_brainlive_event_assembly_audit_v1514)
    p=sub.add_parser("brainlive-silent-life-mine"); p.add_argument("--person-id"); p.add_argument("--package-date"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--timeout", type=float, default=120.0); p.add_argument("--transcript-char-threshold", type=int, default=80); p.add_argument("--no-export", action="store_true"); p.add_argument("--limit", type=int, default=200); p.set_defaults(func=cmd_brainlive_silent_life_mine_v160)
    p=sub.add_parser("brainlive-silent-life-audit"); p.add_argument("--person-id"); p.add_argument("--package-date"); p.set_defaults(func=cmd_brainlive_silent_life_audit_v160)
    p=sub.add_parser("brainlive-deep-vision-run"); p.add_argument("--person-id"); p.add_argument("--package-date"); p.add_argument("--model"); p.add_argument("--timeout-per-image", type=float, default=45.0); p.add_argument("--max-keyframes-per-bundle", type=int, default=12); p.add_argument("--transcript-char-threshold", type=int); p.add_argument("--limit-bundles", type=int, default=200); p.add_argument("--no-append-to-brain2", action="store_true"); p.add_argument("--fail-on-vlm-error", action="store_true"); p.add_argument("--no-vlm", action="store_true"); p.set_defaults(func=cmd_brainlive_deep_vision_run_v161)
    p=sub.add_parser("brainlive-deep-vision-audit"); p.add_argument("--person-id"); p.add_argument("--package-date"); p.set_defaults(func=cmd_brainlive_deep_vision_audit_v161)
    p=sub.add_parser("brainlive-deep-audio-run"); p.add_argument("--person-id", required=True); p.add_argument("--package-date", required=True); p.add_argument("--live-session-id"); p.add_argument("--language", default="fr"); p.add_argument("--max-bundle-audio-seconds", type=float, default=1800.0); p.set_defaults(func=cmd_brainlive_deep_audio_run_v185)
    p=sub.add_parser("brainlive-deep-audio-audit"); p.add_argument("--person-id", required=True); p.add_argument("--package-date"); p.set_defaults(func=cmd_brainlive_deep_audio_audit_v185)
    p=sub.add_parser("brainlive-post-stop-flow"); p.add_argument("--person-id"); p.add_argument("--live-session-id"); p.add_argument("--service-run-id"); p.add_argument("--package-date"); p.add_argument("--limit-per-table", type=int, default=5000); p.add_argument("--gap-minutes", type=int, default=20); p.add_argument("--force", action="store_true"); p.add_argument("--no-brain2", action="store_true"); p.add_argument("--no-v15", action="store_true"); p.add_argument("--no-llm", action="store_true"); p.add_argument("--no-silent-life", action="store_true"); p.add_argument("--silent-life-timeout", type=float, default=120.0); p.add_argument("--no-deep-audio", action="store_true", help="Skip the required V18.5 WhisperX/Pyannote bundle refinement."); p.add_argument("--deep-audio-language", default="fr"); p.add_argument("--deep-audio-max-bundle-seconds", type=float, default=1800.0); p.add_argument("--no-deep-vision", action="store_true"); p.add_argument("--deep-vision-model"); p.add_argument("--deep-vision-timeout-per-image", type=float, default=45.0); p.add_argument("--deep-vision-max-keyframes-per-bundle", type=int, default=12); p.set_defaults(func=cmd_brainlive_post_stop_flow_v1515)
    p=sub.add_parser("brainlive-post-stop-flow-audit"); p.add_argument("--person-id"); p.add_argument("--package-date"); p.set_defaults(func=cmd_brainlive_post_stop_flow_audit_v1515)

    p=sub.add_parser("v18-legacy-migrate"); p.add_argument("--person-id"); p.add_argument("--apply", action="store_true", help="Write only safe scope proofs and quarantine structural contradictions."); p.add_argument("--limit", type=int, default=5000); p.set_defaults(func=cmd_v18_legacy_migrate)
    p=sub.add_parser("v18-legacy-forecast-reconcile"); p.add_argument("--person-id", required=True); p.set_defaults(func=cmd_v18_legacy_forecast_reconcile)
    p=sub.add_parser("v18-legacy-forecast-outcome"); p.add_argument("source_table", choices=["v14_trajectory_forecasts","v14_forecast_watch_queue"]); p.add_argument("source_id"); p.add_argument("--person-id", required=True); outcome=p.add_mutually_exclusive_group(); outcome.add_argument("--correct", action="store_true"); outcome.add_argument("--incorrect", action="store_true"); p.add_argument("--evidence", help="JSON evidence for the explicit outcome."); p.set_defaults(func=cmd_v18_legacy_forecast_outcome)
    p=sub.add_parser("v18-legacy-forecast-audit"); p.add_argument("--person-id", required=True); p.set_defaults(func=cmd_v18_legacy_forecast_audit)
    p=sub.add_parser("v18-poststop-cleanup-check"); p.add_argument("run_id"); p.add_argument("--person-id", required=True); p.set_defaults(func=cmd_v18_poststop_cleanup_check)
    p=sub.add_parser("v18-release-audit"); p.add_argument("--stale-after-seconds", type=int, default=600); p.add_argument("--strict", action="store_true", help="Treat unresolved warnings as a failed release gate."); p.add_argument("--fail", action="store_true", help="Exit 2 unless the audit status is exactly ok."); p.set_defaults(func=cmd_v18_release_audit)
    p=sub.add_parser("integrity-v176-migrate"); p.set_defaults(func=cmd_integrity_v176_migrate)
    p=sub.add_parser("integrity-v176-audit"); p.add_argument("--fail", action="store_true"); p.set_defaults(func=cmd_integrity_v176_audit)
    p=sub.add_parser("doctor"); p.set_defaults(func=cmd_doctor)
    for _name, _help in [
        ("doctor-core-v18-8", "Strict V18.8 core-profile readiness gate (no Graphiti/Mem0)."),
        ("doctor-core-v18-7", "Deprecated compatibility alias; use doctor-core-v18-8."),
        ("doctor-core-v18-6", "Deprecated compatibility alias; use doctor-core-v18-8."),
    ]:
        p=sub.add_parser(_name, help=_help); p.add_argument("--fail", action="store_true"); p.add_argument("--check-models", action="store_true", help="Load and probe installed local models sequentially."); p.add_argument("--check-vectors", action="store_true", help="Also load embedding/reranker after audio probes."); p.add_argument("--check-bridge", action="store_true", help="Require the configured Phone Bridge health endpoint."); p.add_argument("--check-bridge-delivery", action="store_true", help="Upload and deliver an isolated fixture through the Bridge queue."); p.add_argument("--no-services", action="store_true", help="Skip Qdrant/Ollama HTTP service checks."); p.set_defaults(func=cmd_doctor_core_v187)
    p=sub.add_parser("brainlive-resume-close-day", help="Resume V18.8 close-day from persisted checkpoints."); p.add_argument("--person-id", required=True); p.add_argument("--package-date"); p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_resume_close_day_v187)
    p=sub.add_parser("brainlive-runtime-status", help="Read the durable detached-service runtime manifest."); p.set_defaults(func=cmd_runtime_status_v187)
    p=sub.add_parser("brainlive-recovery-status", help="Refuse a new capture when V18.8 recovery work is still pending."); p.add_argument("--person-id", required=True); p.set_defaults(func=cmd_recovery_status_v187)
    p=sub.add_parser("brainlive-recover-stale-services", help="Mark crashed/stale service heartbeats orphaned so close-day can resume safely."); p.add_argument("--stale-after-seconds", type=int); p.set_defaults(func=cmd_recover_stale_services_v187)
    p=sub.add_parser("brainlive-resume-inbox-drain", help="Drain retained inbox safely after a crash before resuming post-stop."); p.add_argument("--person-id", required=True); p.add_argument("--live-session-id"); p.add_argument("--service-run-id"); p.set_defaults(func=cmd_resume_inbox_drain_v187)
    p=sub.add_parser("doctor-elite"); p.add_argument("--fail", action="store_true"); p.set_defaults(func=cmd_doctor_elite)
    args=parser.parse_args(argv); args.func(args)

if __name__ == "__main__": main()
