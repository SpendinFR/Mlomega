from __future__ import annotations

"""Self voice + active unknown-voice learning.

This module is intentionally not a cognitive analyst. It only handles speaker
identity from acoustic embeddings and explicit user corrections.

Core contract:
- person_id='me' / is_user=true must be enrolled explicitly via setup_me.
- audio ingestion compares diarized speaker samples to known voices.
- unknown recurring voices are clustered as UNKNOWN_VOICE_xxx.
- once the user names a cluster, past rows are relabelled retroactively while
  preserving raw speaker labels and original source text.
"""

import json
import math
import tempfile
from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, init_db, upsert
from .utils import json_dumps, json_loads, now_iso, sha256_file, stable_id
from .vector_memory import cosine
from .voice_identity import SpeechBrainVoiceEmbedder, VoiceIdentityError, ensure_speaker, enroll_voice


class VoiceLearningError(RuntimeError):
    pass


AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".mp4", ".mov"}


def ensure_voice_learning_schema() -> None:
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS self_voice_profile(
                person_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                is_user INTEGER NOT NULL DEFAULT 1,
                primary_embedding_id TEXT,
                setup_audio_path TEXT,
                setup_audio_sha256 TEXT,
                setup_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_clusters(
                cluster_id TEXT PRIMARY KEY,
                canonical_person_id TEXT,
                display_label TEXT NOT NULL,
                status TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                observation_count INTEGER NOT NULL DEFAULT 0,
                total_duration_s REAL NOT NULL DEFAULT 0,
                often_with_user_count INTEGER NOT NULL DEFAULT 0,
                prompt_status TEXT NOT NULL DEFAULT 'not_needed',
                prompt_after_count INTEGER NOT NULL DEFAULT 5,
                prompt_after_duration_s REAL NOT NULL DEFAULT 420,
                centroid_embedding_json TEXT,
                model TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_observations(
                observation_id TEXT PRIMARY KEY,
                cluster_id TEXT,
                conversation_id TEXT,
                speaker_label TEXT,
                source_audio_path TEXT,
                sample_path TEXT,
                start_s REAL,
                end_s REAL,
                duration_s REAL,
                embedding_json TEXT,
                model TEXT,
                best_known_person_id TEXT,
                best_known_score REAL,
                best_cluster_id TEXT,
                best_cluster_score REAL,
                decision TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_identity_revisions(
                revision_id TEXT PRIMARY KEY,
                cluster_id TEXT,
                old_person_id TEXT,
                new_person_id TEXT NOT NULL,
                display_name TEXT,
                reason TEXT,
                rows_updated_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_pending_prompts(
                prompt_id TEXT PRIMARY KEY,
                cluster_id TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                observation_count INTEGER NOT NULL,
                total_duration_s REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                answered_at TEXT
            );
            """
        )
        con.commit()


def setup_me(audio_path: Path, *, display_name: str = "Moi / Will", person_id: str = "me") -> dict[str, Any]:
    """Enroll the project owner as the central user voice."""
    ensure_voice_learning_schema()
    embedding_id = enroll_voice(person_id, Path(audio_path), display_name=display_name, is_user=True)
    p = Path(audio_path).expanduser().resolve()
    now = now_iso()
    with connect() as con:
        upsert(con, "self_voice_profile", {
            "person_id": person_id,
            "display_name": display_name,
            "is_user": 1,
            "primary_embedding_id": embedding_id,
            "setup_audio_path": str(p),
            "setup_audio_sha256": sha256_file(p),
            "setup_status": "active",
            "created_at": now,
            "updated_at": now,
        }, "person_id")
        con.commit()
    return {"status": "ok", "person_id": person_id, "display_name": display_name, "embedding_id": embedding_id, "is_user": True}


def _self_voice_ready() -> bool:
    ensure_voice_learning_schema()
    with connect() as con:
        return bool(con.execute("SELECT 1 FROM speaker_profiles sp JOIN voice_embeddings ve ON ve.person_id=sp.person_id WHERE sp.is_user=1 LIMIT 1").fetchone())


def _next_unknown_cluster_id(con) -> str:
    n = con.execute("SELECT COUNT(*) AS c FROM voice_clusters").fetchone()["c"] + 1
    while True:
        cid = f"UNKNOWN_VOICE_{n:03d}"
        if not con.execute("SELECT 1 FROM voice_clusters WHERE cluster_id=?", (cid,)).fetchone():
            return cid
        n += 1


def _mean_embedding(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    length = min(len(v) for v in vectors)
    out = [sum(v[i] for v in vectors) / len(vectors) for i in range(length)]
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


def _best_known_match(con, vector: list[float]) -> tuple[str | None, float, str | None]:
    best_person: str | None = None
    best_model: str | None = None
    best_score = -1.0
    for row in con.execute("SELECT person_id, embedding_json, model FROM voice_embeddings"):
        try:
            score = cosine(vector, json.loads(row["embedding_json"]))
        except Exception:
            continue
        if score > best_score:
            best_score = float(score)
            best_person = row["person_id"]
            best_model = row["model"]
    return best_person, best_score if best_score >= 0 else 0.0, best_model


def _best_cluster_match(con, vector: list[float]) -> tuple[str | None, float]:
    best_id: str | None = None
    best_score = -1.0
    for row in con.execute("SELECT cluster_id, centroid_embedding_json FROM voice_clusters WHERE canonical_person_id IS NULL AND centroid_embedding_json IS NOT NULL"):
        try:
            score = cosine(vector, json.loads(row["centroid_embedding_json"]))
        except Exception:
            continue
        if score > best_score:
            best_score = float(score)
            best_id = row["cluster_id"]
    return best_id, best_score if best_score >= 0 else 0.0


def _extract_label_sample(audio_path: Path, label: str, segments: list[dict[str, Any]], *, conversation_id: str | None = None, max_seconds: float = 60.0) -> tuple[Path, float, float | None, float | None]:
    """Extract a representative per-speaker sample using real diarized timestamps."""
    import torch
    import torchaudio

    audio_path = audio_path.expanduser().resolve()
    wav, sr = torchaudio.load(str(audio_path))
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    chunks = []
    total = 0.0
    first_start: float | None = None
    last_end: float | None = None
    for seg in segments:
        if (seg.get("speaker") or "SPEAKER_UNKNOWN") != label:
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or start)
        if end <= start:
            continue
        i0 = max(0, int(start * sr))
        i1 = min(wav.shape[1], int(end * sr))
        if i1 <= i0:
            continue
        piece = wav[:, i0:i1]
        chunks.append(piece)
        total += (i1 - i0) / sr
        first_start = start if first_start is None else min(first_start, start)
        last_end = end if last_end is None else max(last_end, end)
        if total >= max_seconds:
            break
    if not chunks:
        raise VoiceLearningError(f"Aucun segment audio exploitable pour {label}")
    sample = torch.cat(chunks, dim=1)
    settings = get_settings()
    out_dir = settings.root_dir / "voice_samples" / (conversation_id or "manual")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{label}.wav"
    torchaudio.save(str(out), sample.cpu(), sr)
    return out, total, first_start, last_end


def resolve_speakers_for_audio(audio_path: Path, segments: list[dict[str, Any]], *, conversation_id: str | None = None, require_self_voice: bool = True, known_threshold: float | None = None, cluster_threshold: float = 0.68) -> dict[str, Any]:
    """Resolve diarized labels to known people or recurring UNKNOWN_VOICE clusters.

    Returns {speaker_map, details}. This is called directly during audio ingestion.
    """
    ensure_voice_learning_schema()
    settings = get_settings()
    known_threshold = settings.voice_threshold if known_threshold is None else known_threshold
    if require_self_voice and not _self_voice_ready():
        raise VoiceLearningError("SELF VOICE non configurée. Lance d'abord: mlomega-audio setup-me ma_voix.wav --display-name Will")
    labels = sorted({seg.get("speaker") or "SPEAKER_UNKNOWN" for seg in segments})
    embedder = SpeechBrainVoiceEmbedder(device=settings.whisperx_device)
    speaker_map: dict[str, str] = {}
    details: list[dict[str, Any]] = []
    now = now_iso()
    with connect() as con:
        for label in labels:
            sample_path, dur, start_s, end_s = _extract_label_sample(Path(audio_path), label, segments, conversation_id=conversation_id)
            vector = embedder.embed_file(sample_path)
            best_person, best_score, best_model = _best_known_match(con, vector)
            best_cluster, best_cluster_score = _best_cluster_match(con, vector)
            decision = "unknown_cluster"
            cluster_id = None
            person_id = None
            if best_person and best_score >= known_threshold:
                person_id = best_person
                decision = "known_person_match"
                ensure_speaker(person_id, display_name=person_id, is_user=bool(con.execute("SELECT is_user FROM speaker_profiles WHERE person_id=?", (person_id,)).fetchone()["is_user"]))
            else:
                if best_cluster and best_cluster_score >= cluster_threshold:
                    cluster_id = best_cluster
                else:
                    cluster_id = _next_unknown_cluster_id(con)
                    upsert(con, "voice_clusters", {
                        "cluster_id": cluster_id,
                        "canonical_person_id": None,
                        "display_label": cluster_id,
                        "status": "unknown",
                        "first_seen_at": now,
                        "last_seen_at": now,
                        "observation_count": 0,
                        "total_duration_s": 0.0,
                        "often_with_user_count": 0,
                        "prompt_status": "not_needed",
                        "prompt_after_count": 5,
                        "prompt_after_duration_s": 420.0,
                        "centroid_embedding_json": json.dumps(vector),
                        "model": "speechbrain-ecapa-voxceleb",
                        "confidence": 0.5,
                        "metadata_json": json_dumps({}),
                        "created_at": now,
                        "updated_at": now,
                    }, "cluster_id")
                person_id = cluster_id
                ensure_speaker(person_id, display_name=person_id, is_user=False)
                # update cluster centroid and counters
                row = con.execute("SELECT centroid_embedding_json, observation_count, total_duration_s FROM voice_clusters WHERE cluster_id=?", (cluster_id,)).fetchone()
                prev_vec = json_loads(row["centroid_embedding_json"], []) if row else []
                prev_count = int(row["observation_count"] or 0) if row else 0
                vectors = ([prev_vec] * max(prev_count, 1) if prev_vec else []) + [vector]
                centroid = _mean_embedding(vectors)
                obs_count = prev_count + 1
                total_duration = (float(row["total_duration_s"] or 0.0) if row else 0.0) + dur
                prompt_status = "ready_to_ask" if obs_count >= 5 or total_duration >= 420 else "not_needed"
                upsert(con, "voice_clusters", {
                    "cluster_id": cluster_id,
                    "canonical_person_id": None,
                    "display_label": cluster_id,
                    "status": "unknown",
                    "first_seen_at": row["first_seen_at"] if row and "first_seen_at" in row.keys() else now,
                    "last_seen_at": now,
                    "observation_count": obs_count,
                    "total_duration_s": total_duration,
                    "often_with_user_count": 0,
                    "prompt_status": prompt_status,
                    "prompt_after_count": 5,
                    "prompt_after_duration_s": 420.0,
                    "centroid_embedding_json": json.dumps(centroid),
                    "model": "speechbrain-ecapa-voxceleb",
                    "confidence": max(float(best_cluster_score or 0), 0.5),
                    "metadata_json": json_dumps({"last_sample_path": str(sample_path)}),
                    "created_at": row["created_at"] if row and "created_at" in row.keys() else now,
                    "updated_at": now,
                }, "cluster_id")
                if prompt_status == "ready_to_ask":
                    prompt_id = stable_id("voiceprompt", cluster_id)
                    upsert(con, "voice_pending_prompts", {
                        "prompt_id": prompt_id,
                        "cluster_id": cluster_id,
                        "prompt_text": f"J'entends souvent cette voix inconnue ({cluster_id}). C'est qui ?",
                        "observation_count": obs_count,
                        "total_duration_s": total_duration,
                        "status": "open",
                        "created_at": now,
                        "answered_at": None,
                    }, "prompt_id")
            speaker_map[label] = person_id or label
            obs_id = stable_id("voiceobs", conversation_id or "manual", label, str(sample_path), now)
            upsert(con, "voice_observations", {
                "observation_id": obs_id,
                "cluster_id": cluster_id,
                "conversation_id": conversation_id,
                "speaker_label": label,
                "source_audio_path": str(Path(audio_path).expanduser().resolve()),
                "sample_path": str(sample_path),
                "start_s": start_s,
                "end_s": end_s,
                "duration_s": dur,
                "embedding_json": json.dumps(vector),
                "model": best_model or "speechbrain-ecapa-voxceleb",
                "best_known_person_id": best_person,
                "best_known_score": best_score,
                "best_cluster_id": best_cluster,
                "best_cluster_score": best_cluster_score,
                "decision": decision,
                "created_at": now,
            }, "observation_id")
            details.append({"speaker_label": label, "person_id": speaker_map[label], "decision": decision, "known_score": best_score, "cluster_id": cluster_id, "duration_s": dur})
        con.commit()
    return {"speaker_map": speaker_map, "details": details}


def pending_unknown_voices() -> dict[str, Any]:
    ensure_voice_learning_schema()
    with connect() as con:
        clusters = [dict(r) for r in con.execute("SELECT * FROM voice_clusters WHERE canonical_person_id IS NULL ORDER BY prompt_status DESC, observation_count DESC, total_duration_s DESC")]
        prompts = [dict(r) for r in con.execute("SELECT * FROM voice_pending_prompts WHERE status='open' ORDER BY created_at")]
    return {"unknown_clusters": clusters, "open_prompts": prompts}


def name_unknown_voice(cluster_id: str, person_id: str, *, display_name: str | None = None, is_user: bool = False, reason: str = "user_named_unknown_voice") -> dict[str, Any]:
    """Name UNKNOWN_VOICE_xxx and relabel past source rows retroactively."""
    ensure_voice_learning_schema()
    now = now_iso()
    ensure_speaker(person_id, display_name or person_id, is_user=is_user)
    with connect() as con:
        cluster = con.execute("SELECT * FROM voice_clusters WHERE cluster_id=?", (cluster_id,)).fetchone()
        if not cluster:
            raise VoiceLearningError(f"cluster introuvable: {cluster_id}")
        old_person_id = cluster_id
        rows_updated: dict[str, int] = {}
        # Main evidence-bearing tables. Raw text and speaker_label are preserved.
        update_specs = [
            ("turns", "person_id"),
            ("source_spans", "person_id"),
            ("source_items", "author_person_id"),
            ("lifestream_segments", "speaker_person_id"),
            ("memory_cards", "person_id"),
            ("memory_evidence", "person_id"),
            ("retrieval_chunks", "person_id"),
            ("speaker_uncertainty_segments", "person_id"),
            ("episodes", "target_person_id"),
            ("interaction_episodes", "other_person_id"),
            ("relationship_models", "person_b"),
        ]
        for table, col in update_specs:
            try:
                cur = con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (person_id, old_person_id))
                rows_updated[f"{table}.{col}"] = cur.rowcount
            except Exception:
                rows_updated[f"{table}.{col}"] = -1
        con.execute("UPDATE voice_clusters SET canonical_person_id=?, display_label=?, status='named', prompt_status='answered', updated_at=? WHERE cluster_id=?", (person_id, display_name or person_id, now, cluster_id))
        con.execute("UPDATE voice_pending_prompts SET status='answered', answered_at=? WHERE cluster_id=? AND status='open'", (now, cluster_id))
        # Promote centroid as a known embedding for next automatic recognition.
        if cluster["centroid_embedding_json"]:
            emb_id = stable_id("voice", person_id, cluster_id, "cluster-centroid")
            upsert(con, "voice_embeddings", {
                "embedding_id": emb_id,
                "person_id": person_id,
                "source_path": f"cluster:{cluster_id}",
                "embedding_json": cluster["centroid_embedding_json"],
                "model": cluster["model"] or "speechbrain-ecapa-voxceleb",
                "confidence": 0.78,
                "created_at": now,
            }, "embedding_id")
        rev_id = stable_id("voicerev", cluster_id, person_id, now)
        upsert(con, "voice_identity_revisions", {
            "revision_id": rev_id,
            "cluster_id": cluster_id,
            "old_person_id": old_person_id,
            "new_person_id": person_id,
            "display_name": display_name or person_id,
            "reason": reason,
            "rows_updated_json": json_dumps(rows_updated),
            "created_at": now,
        }, "revision_id")
        try:
            upsert(con, "model_revisions", {
                "model_revision_id": stable_id("modelrev", "voice", rev_id),
                "target_table": "speaker_profiles",
                "target_id": person_id,
                "revision_type": "voice_identity_retroactive_relabel",
                "previous_json": json_dumps({"person_id": old_person_id}),
                "new_json": json_dumps({"person_id": person_id, "display_name": display_name or person_id, "is_user": is_user}),
                "reason": reason,
                "evidence_json": json_dumps([{"cluster_id": cluster_id, "rows_updated": rows_updated}]),
                "created_at": now,
            }, "model_revision_id")
        except Exception:
            pass
        con.commit()
    return {"status": "ok", "cluster_id": cluster_id, "person_id": person_id, "display_name": display_name or person_id, "is_user": is_user, "rows_updated": rows_updated}
