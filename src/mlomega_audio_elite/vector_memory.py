from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Iterable, Any

from .config import get_settings
from .runtime_v18_7 import cached_resource


class EliteVectorError(RuntimeError):
    pass


class SentenceTransformerEmbedder:
    """Mandatory local elite embeddings.

    This class intentionally has no hash/BOW mode dégradé. If sentence-transformers,
    the configured model, or the CUDA runtime is unavailable, startup fails loudly
    instead of silently degrading memory quality.
    """
    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.device = device or settings.whisperx_device
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:  # pragma: no cover - requires elite deps
            raise EliteVectorError("sentence-transformers absent. Installe requirements-rtx3070.txt") from exc
        try:
            self.model = SentenceTransformer(self.model_name, device=self.device, trust_remote_code=True)
        except Exception as exc:  # pragma: no cover - model/GPU dependent
            raise EliteVectorError(f"Impossible de charger l'embedder élite '{self.model_name}' sur {self.device}: {exc}") from exc
        dim = self.model.get_sentence_embedding_dimension()
        if not dim:
            raise EliteVectorError(f"Dimension d'embedding inconnue pour {self.model_name}")
        self.dims = int(dim)

    def embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise EliteVectorError("Texte vide refusé: pas d'embedding élite significatif.")
        vec = self.model.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0]
        return [float(x) for x in vec.tolist()]


class CrossEncoderReranker:
    """Mandatory local cross-encoder reranker for elite retrieval."""
    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        settings = get_settings()
        self.model_name = model_name or settings.reranker_model
        self.device = device or settings.whisperx_device
        try:
            from sentence_transformers import CrossEncoder
        except Exception as exc:  # pragma: no cover - requires elite deps
            raise EliteVectorError("sentence-transformers absent pour reranker. Installe requirements-rtx3070.txt") from exc
        try:
            self.model = CrossEncoder(self.model_name, device=self.device, trust_remote_code=True)
        except Exception as exc:  # pragma: no cover - model/GPU dependent
            raise EliteVectorError(f"Impossible de charger le reranker élite '{self.model_name}' sur {self.device}: {exc}") from exc

    def rerank(self, query: str, candidates: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
        if not candidates:
            return []
        pairs = [(query, c.get("text", "")) for c in candidates]
        scores = self.model.predict(pairs)
        out = []
        for c, s in zip(candidates, scores):
            cc = dict(c)
            cc["rerank_score"] = float(s)
            out.append(cc)
        return sorted(out, key=lambda x: x["rerank_score"], reverse=True)[:limit]


def get_embedder() -> SentenceTransformerEmbedder:
    settings = get_settings()
    if settings.embedding_backend != "sentence_transformers":
        raise EliteVectorError(
            "Backend embedding refusé. Cette build élite exige MLOMEGA_EMBEDDING_BACKEND=sentence_transformers."
        )
    return cached_resource(
        ("embedder", settings.embedding_model, settings.whisperx_device),
        lambda: SentenceTransformerEmbedder(settings.embedding_model, device=settings.whisperx_device),
    )


def get_reranker() -> CrossEncoderReranker:
    settings = get_settings()
    if settings.reranker_backend != "sentence_transformers":
        raise EliteVectorError("Reranking refusé: MLOMEGA_RERANKER_BACKEND doit être sentence_transformers.")
    return cached_resource(
        ("reranker", settings.reranker_model, settings.whisperx_device),
        lambda: CrossEncoderReranker(settings.reranker_model, device=settings.whisperx_device),
    )


def normalize(vec: Iterable[float]) -> list[float]:
    vv = [float(x) for x in vec]
    norm = math.sqrt(sum(v * v for v in vv)) or 1.0
    return [v / norm for v in vv]


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    aa = list(a)
    bb = list(b)
    if not aa or not bb or len(aa) != len(bb):
        return 0.0
    return sum(x * y for x, y in zip(aa, bb)) / ((math.sqrt(sum(x * x for x in aa)) or 1.0) * (math.sqrt(sum(y * y for y in bb)) or 1.0))


def qdrant_point_id(value: str) -> str:
    """Return a deterministic UUID accepted by Qdrant point-ID validation.

    Existing application identifiers intentionally use readable stable prefixes
    such as ``vector_point_v18_…``.  Qdrant only accepts an integer or UUID as
    the transport point ID, so the original identity remains in payload/SQLite
    while this function provides a deterministic transport key.
    """
    raw = str(value or "").strip()
    if not raw:
        raise EliteVectorError("Qdrant point id cannot be empty")
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mlomega/qdrant/{raw}"))


@dataclass
class VectorPoint:
    point_id: str
    vector: list[float]
    payload: dict[str, Any]


class QdrantMemoryStore:
    def __init__(self, collection: str | None = None, vector_size: int | None = None) -> None:
        settings = get_settings()
        self.collection = collection or settings.qdrant_collection
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
        except Exception as exc:  # pragma: no cover - requires elite deps
            raise EliteVectorError("qdrant-client absent. Installe requirements-rtx3070.txt") from exc
        self._models = __import__("qdrant_client.models", fromlist=["PointStruct"])
        try:
            self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
            collections = [c.name for c in self.client.get_collections().collections]
            if vector_size and self.collection not in collections:
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                )
            elif vector_size:
                # A collection with another embedding dimension is not an
                # interchangeable memory. Fail loudly instead of inserting
                # vectors that Qdrant will reject or interpreting with a
                # different model. The dedicated V17 collection avoids this
                # class of migration entirely.
                info = self.client.get_collection(collection_name=self.collection)
                vectors = getattr(getattr(info, "config", None), "params", None)
                vectors = getattr(vectors, "vectors", None)
                size = getattr(vectors, "size", None)
                if size is not None and int(size) != int(vector_size):
                    raise EliteVectorError(
                        f"Qdrant collection {self.collection!r} has dimension {size}, expected {vector_size}."
                    )
        except EliteVectorError:
            raise
        except Exception as exc:  # pragma: no cover - service dependent
            raise EliteVectorError(f"Qdrant indisponible sur {settings.qdrant_url}: {exc}") from exc

    def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        PointStruct = self._models.PointStruct
        self.client.upsert(
            collection_name=self.collection,
            points=[PointStruct(id=qdrant_point_id(p.point_id), vector=p.vector, payload=p.payload) for p in points],
        )

    def ensure_payload_indexes(self, fields: Iterable[tuple[str, str]]) -> None:
        """Create filter indexes required by a scoped Qdrant projection.

        Failing to create one must be visible: a production query that relies on
        owner/time filters must not silently degrade to an unindexed broad scan.
        Qdrant treats repeated creation as idempotent.
        """
        try:
            from qdrant_client.models import PayloadSchemaType
            for field, kind in fields:
                schema = getattr(PayloadSchemaType, str(kind).upper())
                self.client.create_payload_index(
                    collection_name=self.collection, field_name=str(field), field_schema=schema
                )
        except Exception as exc:  # pragma: no cover - service/version boundary
            raise EliteVectorError(f"Unable to create Qdrant payload indexes for {self.collection}: {exc}") from exc

    def search(self, vector: list[float], limit: int = 20, query_filter: Any | None = None) -> list[dict[str, Any]]:
        try:
            res = self.client.query_points(collection_name=self.collection, query=vector, limit=limit, query_filter=query_filter)
            points = getattr(res, "points", res)
        except AttributeError:
            points = self.client.search(collection_name=self.collection, query_vector=vector, limit=limit, query_filter=query_filter)
        return [{"id": str(p.id), "score": float(p.score), "payload": dict(p.payload or {})} for p in points]


class LanceDBMemoryStore:
    def __init__(self, table_name: str = "mlomega_audio_memory") -> None:
        settings = get_settings()
        if settings.vector_backend != "lancedb":
            raise EliteVectorError("LanceDB uniquement si MLOMEGA_VECTOR_BACKEND=lancedb.")
        try:
            import lancedb
        except Exception as exc:  # pragma: no cover - requires elite deps
            raise EliteVectorError("lancedb absent. Installe requirements-rtx3070.txt") from exc
        self.db = lancedb.connect(settings.lancedb_uri)
        self.table_name = table_name
        self.table = None

    def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        data = [{"id": p.point_id, "vector": p.vector, **p.payload} for p in points]
        if self.table_name in self.db.table_names():
            self.table = self.db.open_table(self.table_name)
            self.table.add(data)
        else:
            self.table = self.db.create_table(self.table_name, data=data)

    def search(self, vector: list[float], limit: int = 20) -> list[dict[str, Any]]:
        if self.table is None:
            self.table = self.db.open_table(self.table_name)
        rows = self.table.search(vector).limit(limit).to_list()
        return [{"id": str(r.get("id")), "score": float(r.get("_distance", 0.0)), "payload": r} for r in rows]


def get_vector_store(vector_size: int | None = None):
    settings = get_settings()
    if settings.vector_backend == "qdrant":
        return cached_resource(
            ("qdrant", settings.qdrant_url, settings.qdrant_collection, str(vector_size or "")),
            lambda: QdrantMemoryStore(vector_size=vector_size),
        )
    if settings.vector_backend == "lancedb":
        return cached_resource(("lancedb", settings.lancedb_uri), lambda: LanceDBMemoryStore())
    raise EliteVectorError("Backend vectoriel refusé. Utilise qdrant ou lancedb, sans chemin local simplifié.")
