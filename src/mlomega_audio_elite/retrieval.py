from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import get_settings
from .llm import OllamaJsonClient
from .vector_memory import get_embedder, get_reranker, get_vector_store, EliteVectorError


class EliteRetrievalError(RuntimeError):
    pass


@dataclass
class SearchHit:
    source_type: str
    source_id: str
    text: str
    score: float
    reason: str
    metadata: dict[str, Any]



def _is_retrievable_payload(payload: dict[str, Any]) -> bool:
    inactive = {"deleted", "invalidated", "retracted", "superseded", "obsolete"}
    lifecycle = str(payload.get("lifecycle_status") or "active").lower()
    event_status = str(payload.get("event_status") or "").lower()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    meta_lifecycle = str(metadata.get("lifecycle_status") or "").lower()
    return lifecycle not in inactive and event_status not in inactive and meta_lifecycle not in inactive


def _search_vector_elite(query: str, limit: int = 50, *, person_id: str | None = None) -> list[SearchHit]:
    settings = get_settings()
    if settings.vector_backend not in {"qdrant", "lancedb"}:
        raise EliteRetrievalError("Recherche refusée: backend vectoriel élite requis (qdrant ou lancedb).")
    embedder = get_embedder()
    qv = embedder.embed(query)
    store = get_vector_store(vector_size=embedder.dims)
    points = store.search(qv, limit=limit)
    hits: list[SearchHit] = []
    for p in points:
        payload = p.get("payload", {})
        if not _is_retrievable_payload(payload):
            continue
        # Vector stores are global by design; caller scope is mandatory for
        # Brain2/BrainLive so a semantically close memory of another profile
        # cannot enter the prompt.
        if person_id is not None and str(payload.get("person_id") or "") != str(person_id):
            continue
        if str(payload.get("truth_status") or "active").lower() in {"invalidated", "retracted", "superseded", "deleted", "obsolete"}:
            continue
        text = payload.get("text") or payload.get("content") or payload.get("summary") or ""
        hits.append(SearchHit(
            source_type=payload.get("source_type", "vector"),
            source_id=payload.get("source_id", p.get("id", "")),
            text=text,
            score=float(p.get("score", 0.0)),
            reason=f"elite vector {settings.vector_backend} model={embedder.model_name}",
            metadata=payload,
        ))
    return hits


def search(query: str, limit: int = 12, *, person_id: str | None = None) -> list[SearchHit]:
    """Elite retrieval: semantic vector DB + mandatory cross-encoder rerank.

    There is no local lexical mirror and no hash embedding path. Missing Qdrant,
    model weights, CUDA, or reranker raises immediately.
    """
    settings = get_settings()
    candidates = _search_vector_elite(query, limit=max(50, limit * 5), person_id=person_id)
    if not candidates:
        return []
    if settings.reranker_backend != "sentence_transformers":
        raise EliteRetrievalError("Reranking refusé: MLOMEGA_RERANKER_BACKEND doit être sentence_transformers.")
    reranker = get_reranker()
    ranked = reranker.rerank(query, [{"text": h.text, "hit": h} for h in candidates], limit=limit)
    out: list[SearchHit] = []
    for rr in ranked:
        h = rr["hit"]
        h.score = float(rr.get("rerank_score", h.score))
        h.reason += " + elite cross-encoder rerank"
        out.append(h)
    return out[:limit]


def answer(query: str, *, person_id: str | None = None) -> str:
    """Generate a grounded local answer from elite retrieval hits."""
    hits = search(query, limit=10, person_id=person_id)
    if not hits:
        return "Aucun souvenir vectoriel élite pertinent trouvé. Lance d'abord ingest-audio/ingest-transcript avec Qdrant synchronisé."
    context = [
        {
            "rank": i + 1,
            "source_type": h.source_type,
            "source_id": h.source_id,
            "score": h.score,
            "reason": h.reason,
            "text": h.text,
            "metadata": h.metadata,
        }
        for i, h in enumerate(hits)
    ]
    system = (
        "Tu es MemoryLight Omega. Réponds uniquement à partir des souvenirs fournis. "
        "Donne une réponse directe, puis cite les preuves internes utiles. "
        "Ne complète pas avec de l'imagination. Réponds en JSON strict."
    )
    schema = {"answer": "string", "evidence": [{"source_type": "string", "source_id": "string", "why": "string"}]}
    payload = {"question": query, "elite_context": context}
    data = OllamaJsonClient().require_json(system, json.dumps(payload, ensure_ascii=False), schema_hint=schema, timeout=90)
    answer_text = str(data.get("answer", "")).strip()
    evidence = data.get("evidence") or []
    lines = [answer_text or "Réponse LLM vide malgré contexte élite."]
    if isinstance(evidence, list) and evidence:
        lines.append("\nPreuves internes :")
        for e in evidence[:6]:
            if isinstance(e, dict):
                lines.append(f"- {e.get('source_type')}:{e.get('source_id')} — {e.get('why')}")
    return "\n".join(lines)
