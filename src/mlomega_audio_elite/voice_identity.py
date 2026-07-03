from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, upsert
from .utils import now_iso, sha256_file, stable_id
from .vector_memory import cosine


class VoiceIdentityError(RuntimeError):
    pass


_EMBEDDER_CACHE: dict[tuple[str, str], "SpeechBrainVoiceEmbedder"] = {}


class SpeechBrainVoiceEmbedder:
    """Mandatory SpeechBrain ECAPA voice embeddings for elite local voice identity."""
    def __init__(self, model_id: str = "speechbrain/spkrec-ecapa-voxceleb", device: str | None = None) -> None:
        settings = get_settings()
        self.device = device or settings.whisperx_device
        try:
            try:
                from speechbrain.inference.speaker import EncoderClassifier  # speechbrain >=1
            except Exception:
                from speechbrain.pretrained import EncoderClassifier  # older speechbrain
            self.classifier = EncoderClassifier.from_hparams(
                source=model_id,
                savedir=str(settings.root_dir / "models" / "speechbrain_ecapa"),
                run_opts={"device": self.device},
            )
            import torch  # noqa: F401
            import torchaudio  # noqa: F401
        except Exception as exc:  # pragma: no cover - requires GPU deps
            raise VoiceIdentityError(
                "SpeechBrain/torchaudio ou le modèle ECAPA sont indisponibles. "
                "Installe requirements-rtx3070.txt et vérifie CUDA. Aucun chemin vocal simplifié n'existe dans cette build."
            ) from exc

    def embed_file(self, path: Path) -> list[float]:
        import torch
        import torchaudio
        path = path.expanduser().resolve()
        wav, sr = torchaudio.load(str(path))
        wav = wav.mean(dim=0, keepdim=True)
        if sr != 16000:
            wav = torchaudio.functional.resample(wav, sr, 16000)
        with torch.no_grad():
            emb = self.classifier.encode_batch(wav.to(self.device))
        vec = emb.squeeze().detach().cpu().float().tolist()
        norm = math.sqrt(sum(float(v) * float(v) for v in vec)) or 1.0
        return [float(v) / norm for v in vec]


def _extract_voice_embedding(audio_path: Path) -> tuple[list[float], str, float]:
    settings = get_settings()
    if not settings.enable_speechbrain:
        raise VoiceIdentityError("MLOMEGA_ENABLE_SPEECHBRAIN=false refusé: identité vocale élite exige SpeechBrain ECAPA.")
    model_id = os.environ.get("MLOMEGA_SPEECHBRAIN_MODEL", "speechbrain/spkrec-ecapa-voxceleb")
    device = settings.whisperx_device
    key = (model_id, device)
    embedder = _EMBEDDER_CACHE.get(key)
    if embedder is None:
        embedder = SpeechBrainVoiceEmbedder(model_id=model_id, device=device)
        _EMBEDDER_CACHE[key] = embedder
    emb = embedder.embed_file(audio_path)
    return emb, "speechbrain-ecapa-voxceleb", 0.90


def ensure_speaker(person_id: str, display_name: str | None = None, is_user: bool = False) -> None:
    with connect() as con:
        upsert(con, "speaker_profiles", {
            "person_id": person_id,
            "display_name": display_name or person_id,
            "is_user": 1 if is_user else 0,
            "aliases_json": "[]",
            "notes": None,
            "created_at": now_iso(),
        }, "person_id")
        con.commit()


def enroll_voice(person_id: str, audio_path: Path, display_name: str | None = None, is_user: bool = False) -> str:
    """Enroll a real SpeechBrain ECAPA voice profile. No synthetic/hash path."""
    audio_path = audio_path.expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    ensure_speaker(person_id, display_name, is_user)
    emb, model, confidence = _extract_voice_embedding(audio_path)
    embedding_id = stable_id("voice", person_id, str(audio_path), sha256_file(audio_path), model)
    with connect() as con:
        upsert(con, "voice_embeddings", {
            "embedding_id": embedding_id,
            "person_id": person_id,
            "source_path": str(audio_path),
            "embedding_json": json.dumps(emb),
            "model": model,
            "confidence": confidence,
            "created_at": now_iso(),
        }, "embedding_id")
        con.commit()
    return embedding_id


def match_voice(audio_path: Path, threshold: float | None = None, *, include_query_embedding: bool = False, top_k: int = 5) -> dict[str, Any]:
    settings = get_settings()
    threshold = settings.voice_threshold if threshold is None else threshold
    audio_path = audio_path.expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    candidate, model, _ = _extract_voice_embedding(audio_path)
    best: dict[str, Any] = {"person_id": None, "score": 0.0, "method": model, "matched": False, "threshold": threshold, "candidates": []}
    scored: list[dict[str, Any]] = []
    with connect() as con:
        for row in con.execute("SELECT person_id, embedding_json, model FROM voice_embeddings"):
            vec = json.loads(row["embedding_json"])
            score = cosine(candidate, vec)
            scored.append({"person_id": row["person_id"], "score": score, "method": f"{model} vs {row['model']}"})
            if score > float(best["score"]):
                best.update({"person_id": row["person_id"], "score": score, "method": f"{model} vs {row['model']}", "matched": score >= threshold})
    scored.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    best["candidates"] = scored[: max(1, int(top_k or 1))]
    if include_query_embedding:
        best["query_embedding"] = candidate
    return best


def resolve_speaker_label(conversation_id: str, speaker_label: str, person_id: str | None, confidence: float, method: str, evidence: dict | None = None) -> None:
    match_id = stable_id("spmatch", conversation_id, speaker_label, person_id or "unknown")
    with connect() as con:
        upsert(con, "speaker_matches", {
            "match_id": match_id,
            "conversation_id": conversation_id,
            "speaker_label": speaker_label,
            "person_id": person_id,
            "confidence": confidence,
            "method": method,
            "evidence_json": json.dumps(evidence or {}, ensure_ascii=False),
            "created_at": now_iso(),
        }, "match_id")
        con.commit()
