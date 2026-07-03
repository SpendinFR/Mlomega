"""V18 dense, causal predictive retrieval for longitudinal V17 cases.

This module deliberately has no lexical/Jaccard fallback.  A predictive edge is
only allowed when all of these controls hold:

* the case vector lives in the existing Qdrant instance but a *dedicated*,
  versioned collection;
* Qdrant is filtered by owner, active status, embedding revision and event-time;
* each returned point is checked again against SQLite canonical state;
* a dense embedding retrieves candidates and a cross-encoder reranks them;
* a calibration trained on **explicitly verified** historical labels accepts the
  score.  Insufficient or rejected calibration causes abstention, never a
  heuristic score presented as probability.

The SQLite database remains canonical.  Qdrant is a rebuildable projection.
"""
from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .config import get_settings
from .db import connect, write_transaction
from .integrity_v176 import iso_utc, parse_iso_utc
from .utils import json_dumps, json_loads, now_iso, stable_id
from .vector_memory import (
    CrossEncoderReranker,
    EliteVectorError,
    QdrantMemoryStore,
    VectorPoint,
    cosine,
    get_embedder,
)


PREDICTIVE_ENTITY_KIND = "v17_predictive_observed_case"
PREDICTIVE_SCHEMA_REVISION = "v18_dense_predictive_v1"

PREDICTIVE_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS v18_predictive_case_vector_manifest(
  person_id TEXT NOT NULL,
  observed_case_id TEXT NOT NULL,
  embedding_revision TEXT NOT NULL,
  embedding_model TEXT NOT NULL,
  qdrant_collection TEXT NOT NULL,
  qdrant_point_id TEXT NOT NULL,
  source_version TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','synced','retracted','quarantined','failed')),
  error_text TEXT,
  synced_at TEXT,
  retracted_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(person_id, observed_case_id, embedding_revision)
);
CREATE INDEX IF NOT EXISTS idx_v18_predictive_vector_state
  ON v18_predictive_case_vector_manifest(person_id, embedding_revision, state, synced_at);

CREATE TABLE IF NOT EXISTS v18_predictive_similarity_labels(
  label_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  anchor_case_id TEXT NOT NULL,
  similar_case_id TEXT NOT NULL,
  label INTEGER NOT NULL CHECK(label IN (0,1)),
  label_source TEXT NOT NULL CHECK(label_source IN ('human_verified','strict_verifier','import_verified')),
  verified_at TEXT NOT NULL,
  source_revision TEXT,
  notes TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(person_id, anchor_case_id, similar_case_id, label_source)
);
CREATE INDEX IF NOT EXISTS idx_v18_predictive_labels_time
  ON v18_predictive_similarity_labels(person_id, verified_at, anchor_case_id);

CREATE TABLE IF NOT EXISTS v18_predictive_similarity_calibrations(
  calibration_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  embedding_revision TEXT NOT NULL,
  embedding_model TEXT NOT NULL,
  calibration_schema_revision TEXT NOT NULL,
  labels_digest TEXT NOT NULL,
  train_start_at TEXT,
  train_end_at TEXT,
  validation_start_at TEXT,
  validation_end_at TEXT,
  train_samples INTEGER NOT NULL,
  validation_samples INTEGER NOT NULL,
  train_positive INTEGER NOT NULL,
  validation_positive INTEGER NOT NULL,
  threshold REAL,
  validation_precision REAL,
  validation_recall REAL,
  validation_brier REAL,
  status TEXT NOT NULL CHECK(status IN ('accepted','insufficient_data','rejected','failed','superseded')),
  calibration_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(person_id, embedding_revision, labels_digest)
);
CREATE INDEX IF NOT EXISTS idx_v18_predictive_calibration_active
  ON v18_predictive_similarity_calibrations(person_id, embedding_revision, status, updated_at);
"""


class PredictiveRetrievalUnavailable(RuntimeError):
    """Dense predictive retrieval is unavailable; callers must abstain."""


class PredictiveValidationError(ValueError):
    """A supposedly canonical case/label violates predictive invariants."""


@dataclass(frozen=True)
class DenseCandidate:
    observed_case_id: str
    dense_similarity: float
    rerank_score: float
    predictive_text: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CalibrationResult:
    calibration_id: str
    person_id: str
    embedding_revision: str
    embedding_model: str
    status: str
    threshold: float | None
    validation_precision: float | None
    validation_recall: float | None
    validation_brier: float | None
    bins: tuple[dict[str, Any], ...]
    labels_digest: str
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted" and self.threshold is not None

    def probability(self, raw_score: float) -> float | None:
        """Map a reranker score to empirical probability only when accepted."""
        if not self.accepted or not math.isfinite(float(raw_score)):
            return None
        value = float(raw_score)
        for row in self.bins:
            lo = float(row["lo"])
            hi = float(row["hi"])
            if lo <= value <= hi:
                return float(row["probability"])
        if not self.bins:
            return None
        # Scores beyond historical support use the nearest *calibrated* bin,
        # never an arbitrary raw score interpreted as probability.
        first, last = self.bins[0], self.bins[-1]
        return float(first["probability"] if value < float(first["lo"]) else last["probability"])


def _json_digest(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def _qdrant_uuid(*parts: Any) -> str:
    """Qdrant accepts UUID/uint IDs; use a deterministic UUID, not a slug."""
    seed = json_dumps(parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mlomega/v18/predictive/{seed}"))


def _float_epoch(value: str) -> float:
    return parse_iso_utc(value).timestamp()


def _finite(value: Any, *, field: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise PredictiveValidationError(f"{field} must be numeric") from exc
    if not math.isfinite(out):
        raise PredictiveValidationError(f"{field} must be finite")
    return out


def ensure_predictive_schema() -> None:
    """Install the RC5 tables independently of the legacy V17 schemas."""
    with connect() as con, write_transaction(con):
        con.executescript(PREDICTIVE_SCHEMA)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        loaded = json_loads(value, {})
        if isinstance(loaded, Mapping):
            return dict(loaded)
    return {}


def predictive_case_text(case: Mapping[str, Any]) -> str:
    """Return the pre-outcome representation permitted in predictive retrieval.

    ``embedding_text`` is generated from title/context/trigger/action/tags/people
    by V18 longitudinal.  Outcomes and post-event state are purposely excluded.
    This function additionally rejects callers that try to smuggle an explicit
    outcome field into a vector payload.
    """
    text = str(case.get("embedding_text") or "").strip()
    if not text:
        raise PredictiveValidationError("observed case has no predictive embedding_text")
    # The source text is canonical in SQLite.  The following fields are not
    # appended by this function: outcome_summary, emotion_after, state_after.
    return text[:4000]


def _case_required(case: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    person_id = str(case.get("person_id") or "").strip()
    case_id = str(case.get("observed_case_id") or "").strip()
    observed_at = str(case.get("observed_at") or case.get("created_at") or "").strip()
    source_version = str(case.get("source_version") or "").strip()
    if not person_id or not case_id or not observed_at or not source_version:
        raise PredictiveValidationError("case requires person_id, observed_case_id, observed_at and source_version")
    canonical_time = iso_utc(parse_iso_utc(observed_at))
    return person_id, case_id, canonical_time, source_version, predictive_case_text(case)


def _payload_for_case(case: Mapping[str, Any], *, embedding_revision: str) -> dict[str, Any]:
    person_id, case_id, observed_at, source_version, text = _case_required(case)
    return {
        "entity_kind": PREDICTIVE_ENTITY_KIND,
        "schema_revision": PREDICTIVE_SCHEMA_REVISION,
        "person_id": person_id,
        "observed_case_id": case_id,
        "observed_at": observed_at,
        "observed_at_epoch": _float_epoch(observed_at),
        "source_version": source_version,
        "embedding_revision": embedding_revision,
        "active": True,
        "predictive_text": text,
        "case_type": str(case.get("case_type") or ""),
        "source_table": "brain2_observed_cases_v17",
    }


def _case_time(case: Mapping[str, Any]) -> datetime:
    _, _, observed_at, _, _ = _case_required(case)
    return parse_iso_utc(observed_at)


def _safe_payload_field(payload: Mapping[str, Any], key: str) -> str:
    return str(payload.get(key) or "").strip()


class DensePredictiveBackend:
    """Qdrant + dense embeddings + cross-encoder for canonical V17 cases."""

    def __init__(
        self,
        *,
        store: Any,
        embedder: Any,
        reranker: Any,
        collection: str,
        embedding_revision: str,
        embedding_model: str,
        dense_candidate_limit: int,
        rerank_candidate_limit: int,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.collection = str(collection)
        self.embedding_revision = str(embedding_revision)
        self.embedding_model = str(embedding_model)
        self.dense_candidate_limit = max(1, int(dense_candidate_limit))
        self.rerank_candidate_limit = max(1, int(rerank_candidate_limit))

    @classmethod
    def from_settings(cls) -> "DensePredictiveBackend":
        settings = get_settings()
        if settings.vector_backend != "qdrant":
            raise PredictiveRetrievalUnavailable(
                "V17 predictive retrieval requires MLOMEGA_VECTOR_BACKEND=qdrant; LanceDB is not an allowed production fallback."
            )
        if settings.embedding_backend != "sentence_transformers":
            raise PredictiveRetrievalUnavailable(
                "V17 predictive retrieval requires MLOMEGA_EMBEDDING_BACKEND=sentence_transformers."
            )
        if settings.reranker_backend != "sentence_transformers":
            raise PredictiveRetrievalUnavailable(
                "V17 predictive retrieval requires MLOMEGA_RERANKER_BACKEND=sentence_transformers."
            )
        try:
            embedder = get_embedder()
            store = QdrantMemoryStore(
                collection=settings.v17_qdrant_collection,
                vector_size=int(embedder.dims),
            )
            # These are not optional performance hints: the production server
            # enforces owner/revision/active/time filters through these fields.
            store.ensure_payload_indexes((
                ("entity_kind", "keyword"), ("person_id", "keyword"),
                ("embedding_revision", "keyword"), ("active", "bool"),
                ("observed_at_epoch", "float"),
            ))
            reranker = CrossEncoderReranker(device=settings.whisperx_device)
        except EliteVectorError as exc:
            raise PredictiveRetrievalUnavailable(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive integration boundary
            raise PredictiveRetrievalUnavailable(f"unable to initialize Qdrant predictive backend: {exc}") from exc
        return cls(
            store=store,
            embedder=embedder,
            reranker=reranker,
            collection=settings.v17_qdrant_collection,
            embedding_revision=settings.v17_embedding_revision,
            embedding_model=str(getattr(embedder, "model_name", settings.embedding_model)),
            dense_candidate_limit=settings.v17_dense_candidate_limit,
            rerank_candidate_limit=settings.v17_rerank_candidate_limit,
        )

    def _qdrant_filter(self, *, person_id: str, before_epoch: float | None) -> Any | None:
        """Build a server-side Qdrant filter, with local checks still mandatory."""
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue, Range
        except Exception as exc:  # pragma: no cover - qdrant optional in unit tests
            raise PredictiveRetrievalUnavailable("qdrant-client is required for predictive filtering") from exc
        must = [
            FieldCondition(key="entity_kind", match=MatchValue(value=PREDICTIVE_ENTITY_KIND)),
            FieldCondition(key="person_id", match=MatchValue(value=person_id)),
            FieldCondition(key="embedding_revision", match=MatchValue(value=self.embedding_revision)),
            FieldCondition(key="active", match=MatchValue(value=True)),
        ]
        if before_epoch is not None:
            must.append(FieldCondition(key="observed_at_epoch", range=Range(lt=float(before_epoch))))
        return Filter(must=must)

    def _manifest_state(self, *, case: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[bool, str]:
        person_id, case_id, _, source_version, _ = _case_required(case)
        digest = _json_digest(payload)
        with connect() as con:
            row = con.execute(
                """SELECT state,source_version,payload_sha256 FROM v18_predictive_case_vector_manifest
                   WHERE person_id=? AND observed_case_id=? AND embedding_revision=?""",
                (person_id, case_id, self.embedding_revision),
            ).fetchone()
        if row and str(row["state"]) == "synced" and str(row["source_version"]) == source_version and str(row["payload_sha256"]) == digest:
            return True, digest
        return False, digest

    def _mark_manifest(self, *, case: Mapping[str, Any], payload: Mapping[str, Any], state: str, error: str | None = None) -> None:
        person_id, case_id, _, source_version, _ = _case_required(case)
        digest = _json_digest(payload)
        point_id = _qdrant_uuid(person_id, case_id, self.embedding_revision)
        now = now_iso()
        with connect() as con, write_transaction(con):
            con.execute(
                """INSERT INTO v18_predictive_case_vector_manifest(
                      person_id,observed_case_id,embedding_revision,embedding_model,qdrant_collection,qdrant_point_id,
                      source_version,payload_sha256,state,error_text,synced_at,retracted_at,metadata_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(person_id,observed_case_id,embedding_revision) DO UPDATE SET
                      embedding_model=excluded.embedding_model,qdrant_collection=excluded.qdrant_collection,
                      qdrant_point_id=excluded.qdrant_point_id,source_version=excluded.source_version,
                      payload_sha256=excluded.payload_sha256,state=excluded.state,error_text=excluded.error_text,
                      synced_at=excluded.synced_at,retracted_at=excluded.retracted_at,metadata_json=excluded.metadata_json""",
                (
                    person_id, case_id, self.embedding_revision, self.embedding_model, self.collection, point_id,
                    source_version, digest, state, error[:2000] if error else None,
                    now if state == "synced" else None,
                    now if state == "retracted" else None,
                    json_dumps({"schema_revision": PREDICTIVE_SCHEMA_REVISION}),
                ),
            )

    def sync_cases(self, cases: Iterable[Mapping[str, Any]], *, person_id: str | None = None) -> dict[str, Any]:
        """Index active canonical cases and tombstone no-longer-active projections.

        Qdrant cannot participate in SQLite's transaction.  The durable manifest
        records ``pending`` before external I/O, then ``synced``/``retracted``
        only after Qdrant accepts the upsert. A restart sees the pending row and
        retries deterministically.
        """
        material = [dict(c) for c in cases]
        owner = str(person_id or (material[0].get("person_id") if material else "") or "").strip()
        if not owner:
            raise PredictiveValidationError("predictive vector sync requires an explicit owner")
        if any(str(c.get("person_id") or "") != owner for c in material):
            raise PredictiveValidationError("predictive vector sync received mixed owners")
        points: list[VectorPoint] = []
        work: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
        skipped = 0
        active_ids: set[str] = set()
        for case in material:
            payload = _payload_for_case(case, embedding_revision=self.embedding_revision)
            active_ids.add(str(payload["observed_case_id"]))
            already, _ = self._manifest_state(case=case, payload=payload)
            if already:
                skipped += 1
                continue
            self._mark_manifest(case=case, payload=payload, state="pending")
            vector = self.embedder.embed(str(payload["predictive_text"]))
            if not vector:
                self._mark_manifest(case=case, payload=payload, state="quarantined", error="empty dense embedding")
                raise PredictiveRetrievalUnavailable("dense embedder returned an empty vector")
            point_id = _qdrant_uuid(payload["person_id"], payload["observed_case_id"], self.embedding_revision)
            points.append(VectorPoint(point_id=point_id, vector=[float(x) for x in vector], payload=payload))
            work.append((case, payload))

        # A case invalidated/retracted in SQLite must not remain an active point
        # in the dedicated collection. Tombstones keep retries idempotent and
        # make stale external state auditable.
        with connect() as con:
            stale = [dict(r) for r in con.execute(
                """SELECT * FROM v18_predictive_case_vector_manifest
                   WHERE person_id=? AND embedding_revision=? AND state IN ('synced','pending','failed')""",
                (owner, self.embedding_revision),
            ).fetchall() if str(r["observed_case_id"]) not in active_ids]
        dim = int(getattr(self.embedder, "dims", 0) or 0)
        if stale and dim <= 0:
            raise PredictiveRetrievalUnavailable("predictive embedder does not expose vector dimension for tombstones")
        tombstones: list[dict[str, Any]] = []
        for row in stale:
            payload = {
                "entity_kind": PREDICTIVE_ENTITY_KIND, "schema_revision": PREDICTIVE_SCHEMA_REVISION,
                "person_id": owner, "observed_case_id": str(row["observed_case_id"]),
                "embedding_revision": self.embedding_revision, "active": False,
                "source_version": str(row["source_version"]), "source_table": "brain2_observed_cases_v17",
            }
            points.append(VectorPoint(point_id=str(row["qdrant_point_id"]), vector=[0.0] * dim, payload=payload))
            tombstones.append(row)
        if not points:
            return {"indexed": 0, "retracted": 0, "skipped_unchanged": skipped, "collection": self.collection}
        try:
            self.store.upsert(points)
        except Exception as exc:
            for case, payload in work:
                self._mark_manifest(case=case, payload=payload, state="failed", error=str(exc))
            raise PredictiveRetrievalUnavailable(f"Qdrant predictive upsert failed: {exc}") from exc
        for case, payload in work:
            self._mark_manifest(case=case, payload=payload, state="synced")
        if tombstones:
            now = now_iso()
            with connect() as con, write_transaction(con):
                for row in tombstones:
                    con.execute(
                        """UPDATE v18_predictive_case_vector_manifest
                           SET state='retracted',error_text=NULL,retracted_at=?,synced_at=NULL
                           WHERE person_id=? AND observed_case_id=? AND embedding_revision=?""",
                        (now, owner, row["observed_case_id"], self.embedding_revision),
                    )
        return {"indexed": len(work), "retracted": len(tombstones), "skipped_unchanged": skipped, "collection": self.collection}

    def _candidate_payload_valid(
        self,
        payload: Mapping[str, Any],
        *,
        person_id: str,
        before: datetime,
        canonical_case: Mapping[str, Any] | None,
    ) -> bool:
        if _safe_payload_field(payload, "entity_kind") != PREDICTIVE_ENTITY_KIND:
            return False
        if _safe_payload_field(payload, "person_id") != person_id:
            return False
        if _safe_payload_field(payload, "embedding_revision") != self.embedding_revision:
            return False
        if payload.get("active") is not True:
            return False
        try:
            point_time = parse_iso_utc(_safe_payload_field(payload, "observed_at"))
        except Exception:
            return False
        if point_time >= before:
            return False
        if not canonical_case:
            return False
        try:
            c_person, c_id, c_at, c_version, _ = _case_required(canonical_case)
        except PredictiveValidationError:
            return False
        if c_person != person_id or c_id != _safe_payload_field(payload, "observed_case_id"):
            return False
        if parse_iso_utc(c_at) >= before:
            return False
        if c_version != _safe_payload_field(payload, "source_version"):
            return False
        return True

    def retrieve(
        self,
        anchor: Mapping[str, Any],
        *,
        canonical_candidates: Mapping[str, Mapping[str, Any]],
        limit: int | None = None,
    ) -> list[DenseCandidate]:
        person_id, anchor_id, anchor_at, _, query_text = _case_required(anchor)
        anchor_time = parse_iso_utc(anchor_at)
        try:
            vector = self.embedder.embed(query_text)
            points = self.store.search(
                [float(x) for x in vector],
                limit=max(self.dense_candidate_limit, int(limit or 0) * 3, 1),
                query_filter=self._qdrant_filter(person_id=person_id, before_epoch=anchor_time.timestamp()),
            )
        except PredictiveRetrievalUnavailable:
            raise
        except Exception as exc:
            raise PredictiveRetrievalUnavailable(f"Qdrant predictive search failed: {exc}") from exc
        prelim: list[dict[str, Any]] = []
        for point in points:
            payload = point.get("payload") if isinstance(point, Mapping) else None
            if not isinstance(payload, Mapping):
                continue
            cid = _safe_payload_field(payload, "observed_case_id")
            if not cid or cid == anchor_id:
                continue
            canonical = canonical_candidates.get(cid)
            if not self._candidate_payload_valid(payload, person_id=person_id, before=anchor_time, canonical_case=canonical):
                continue
            text = predictive_case_text(canonical)
            score = _finite(point.get("score"), field="qdrant dense score")
            prelim.append({"text": text, "canonical": canonical, "payload": dict(payload), "dense_score": score})
        if not prelim:
            return []
        # The cross-encoder may return non-probabilistic logits; calibration is
        # applied later. Its raw score is never copied into confidence/probability.
        try:
            ranked = self.reranker.rerank(query_text, prelim, limit=min(self.rerank_candidate_limit, int(limit or self.rerank_candidate_limit)))
        except Exception as exc:
            raise PredictiveRetrievalUnavailable(f"predictive cross-encoder rerank failed: {exc}") from exc
        out: list[DenseCandidate] = []
        seen: set[str] = set()
        for row in ranked:
            canonical = row.get("canonical")
            payload = row.get("payload")
            if not isinstance(canonical, Mapping) or not isinstance(payload, Mapping):
                continue
            cid = str(canonical.get("observed_case_id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append(DenseCandidate(
                observed_case_id=cid,
                dense_similarity=_finite(row.get("dense_score"), field="dense score"),
                rerank_score=_finite(row.get("rerank_score"), field="rerank score"),
                predictive_text=str(row.get("text") or ""),
                payload=dict(payload),
            ))
        return out

    def score_pair(self, anchor: Mapping[str, Any], similar: Mapping[str, Any]) -> tuple[float, float]:
        """Use the exact dense/rerank components used by live retrieval for calibration."""
        _, _, _, _, query = _case_required(anchor)
        _, _, _, _, text = _case_required(similar)
        try:
            av = [float(x) for x in self.embedder.embed(query)]
            bv = [float(x) for x in self.embedder.embed(text)]
            dense = _finite(cosine(av, bv), field="dense cosine")
            ranked = self.reranker.rerank(query, [{"text": text, "pair": True, "dense_score": dense}], limit=1)
            if not ranked:
                raise PredictiveRetrievalUnavailable("cross-encoder returned no score for calibration pair")
            rerank = _finite(ranked[0].get("rerank_score"), field="calibration rerank score")
            return dense, rerank
        except PredictiveRetrievalUnavailable:
            raise
        except Exception as exc:
            raise PredictiveRetrievalUnavailable(f"could not score calibration pair: {exc}") from exc


def get_predictive_backend() -> DensePredictiveBackend:
    return DensePredictiveBackend.from_settings()


def register_verified_similarity_label(
    *,
    person_id: str,
    anchor_case_id: str,
    similar_case_id: str,
    label: bool | int,
    label_source: str,
    verified_at: str,
    source_revision: str | None = None,
    notes: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Register a causally valid, explicitly verified calibration label.

    Labels may be created by a human review, strict verifier, or vetted import;
    raw outcome text is deliberately not converted into labels here.
    """
    ensure_predictive_schema()
    owner = str(person_id or "").strip()
    if not owner:
        raise PredictiveValidationError("verified similarity label requires person_id")
    if label_source not in {"human_verified", "strict_verifier", "import_verified"}:
        raise PredictiveValidationError("unsupported verified label source")
    if isinstance(label, bool):
        label_value = int(label)
    elif isinstance(label, int) and label in {0, 1}:
        label_value = int(label)
    else:
        raise PredictiveValidationError("label must be boolean/0/1")
    verified = iso_utc(parse_iso_utc(verified_at))
    with connect() as con, write_transaction(con):
        anchor = con.execute(
            "SELECT person_id,observed_at,source_version FROM brain2_observed_cases_v17 WHERE observed_case_id=?",
            (anchor_case_id,),
        ).fetchone()
        similar = con.execute(
            "SELECT person_id,observed_at,source_version FROM brain2_observed_cases_v17 WHERE observed_case_id=?",
            (similar_case_id,),
        ).fetchone()
        if not anchor or not similar:
            raise PredictiveValidationError("label cases must exist")
        if str(anchor["person_id"]) != owner or str(similar["person_id"]) != owner:
            raise PredictiveValidationError("label cases must belong to requested owner")
        anchor_at = parse_iso_utc(str(anchor["observed_at"]))
        similar_at = parse_iso_utc(str(similar["observed_at"]))
        if similar_at >= anchor_at:
            raise PredictiveValidationError("similar case must be strictly earlier than anchor case")
        if parse_iso_utc(verified) < anchor_at:
            raise PredictiveValidationError("verification cannot precede anchor occurrence")
        label_id = stable_id("predictive_label18", owner, anchor_case_id, similar_case_id, label_source)
        now = now_iso()
        con.execute(
            """INSERT INTO v18_predictive_similarity_labels(
                    label_id,person_id,anchor_case_id,similar_case_id,label,label_source,verified_at,
                    source_revision,notes,metadata_json,created_at,updated_at
                  ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(person_id,anchor_case_id,similar_case_id,label_source) DO UPDATE SET
                    label=excluded.label,verified_at=excluded.verified_at,source_revision=excluded.source_revision,
                    notes=excluded.notes,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (
                label_id, owner, anchor_case_id, similar_case_id, label_value, label_source, verified,
                source_revision or stable_id("case_pair", anchor["source_version"], similar["source_version"]),
                notes[:2000] if notes else None, json_dumps(dict(metadata or {})), now, now,
            ),
        )
    return {"label_id": label_id, "person_id": owner, "anchor_case_id": anchor_case_id, "similar_case_id": similar_case_id, "label": label_value}


def _label_rows(person_id: str) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            """SELECT l.*, a.observed_at AS anchor_observed_at, b.observed_at AS similar_observed_at,
                      a.source_version AS anchor_source_version, b.source_version AS similar_source_version,
                      a.person_id AS anchor_person_id, b.person_id AS similar_person_id,
                      a.embedding_text AS anchor_embedding_text, b.embedding_text AS similar_embedding_text,
                      a.case_type AS anchor_case_type, b.case_type AS similar_case_type,
                      a.status AS anchor_status, b.status AS similar_status,
                      a.invalidated_at AS anchor_invalidated_at, b.invalidated_at AS similar_invalidated_at
               FROM v18_predictive_similarity_labels l
               JOIN brain2_observed_cases_v17 a ON a.observed_case_id=l.anchor_case_id
               JOIN brain2_observed_cases_v17 b ON b.observed_case_id=l.similar_case_id
               WHERE l.person_id=?
               ORDER BY a.observed_at,l.verified_at,l.label_id""",
            (person_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        if r.get("anchor_person_id") != person_id or r.get("similar_person_id") != person_id:
            continue
        if r.get("anchor_status") != "active" or r.get("similar_status") != "active":
            continue
        if r.get("anchor_invalidated_at") or r.get("similar_invalidated_at"):
            continue
        try:
            anchor_t = parse_iso_utc(str(r["anchor_observed_at"]))
            similar_t = parse_iso_utc(str(r["similar_observed_at"]))
            verified_t = parse_iso_utc(str(r["verified_at"]))
        except Exception:
            continue
        if not similar_t < anchor_t <= verified_t:
            continue
        out.append(r)
    return out


def _quantile_boundaries(values: Sequence[float], buckets: int = 5) -> list[float]:
    ordered = sorted(float(x) for x in values)
    if not ordered:
        return []
    if len(ordered) == 1:
        return [ordered[0], ordered[0]]
    boundaries = [ordered[0]]
    for i in range(1, min(buckets, len(ordered))):
        index = int(round((len(ordered) - 1) * i / min(buckets, len(ordered))))
        boundaries.append(ordered[index])
    boundaries.append(ordered[-1])
    return sorted(set(boundaries))


def _build_bins(samples: Sequence[tuple[float, int]], *, buckets: int = 5) -> list[dict[str, Any]]:
    if not samples:
        return []
    bounds = _quantile_boundaries([score for score, _ in samples], buckets=buckets)
    if len(bounds) == 1:
        bounds = [bounds[0], bounds[0]]
    rows: list[dict[str, Any]] = []
    for idx in range(len(bounds) - 1):
        lo, hi = bounds[idx], bounds[idx + 1]
        selected = [(s, y) for s, y in samples if (lo <= s <= hi if idx == len(bounds) - 2 else lo <= s < hi)]
        if not selected:
            continue
        # Laplace smoothing avoids presenting an exact 0/1 probability from a
        # tiny sample while preserving empirical meaning.
        positives = sum(y for _, y in selected)
        probability = (positives + 1.0) / (len(selected) + 2.0)
        rows.append({"lo": lo, "hi": hi, "samples": len(selected), "positives": positives, "probability": probability})
    if not rows:
        positives = sum(y for _, y in samples)
        rows.append({"lo": min(s for s, _ in samples), "hi": max(s for s, _ in samples), "samples": len(samples), "positives": positives, "probability": (positives + 1.0) / (len(samples) + 2.0)})
    # Pool-adjacent-violators in compact form: empirical probability should not
    # decrease as reranker score increases.
    changed = True
    while changed and len(rows) > 1:
        changed = False
        merged: list[dict[str, Any]] = []
        i = 0
        while i < len(rows):
            current = dict(rows[i])
            if i + 1 < len(rows) and float(current["probability"]) > float(rows[i + 1]["probability"]):
                nxt = rows[i + 1]
                samples_total = int(current["samples"]) + int(nxt["samples"])
                positives_total = int(current["positives"]) + int(nxt["positives"])
                current = {
                    "lo": current["lo"], "hi": nxt["hi"], "samples": samples_total,
                    "positives": positives_total, "probability": (positives_total + 1.0) / (samples_total + 2.0),
                }
                i += 1
                changed = True
            merged.append(current)
            i += 1
        rows = merged
    return rows


def _probability_from_bins(bins: Sequence[Mapping[str, Any]], score: float) -> float:
    if not bins:
        return 0.5
    for row in bins:
        if float(row["lo"]) <= score <= float(row["hi"]):
            return float(row["probability"])
    return float(bins[0]["probability"] if score < float(bins[0]["lo"]) else bins[-1]["probability"])


def _best_threshold(samples: Sequence[tuple[float, int]], *, min_support: int) -> float | None:
    if not samples:
        return None
    candidates = sorted({float(score) for score, _ in samples})
    best: tuple[float, float, int] | None = None  # precision, threshold, support
    for threshold in candidates:
        selected = [(s, y) for s, y in samples if s >= threshold]
        if len(selected) < min_support:
            continue
        precision = sum(y for _, y in selected) / len(selected)
        candidate = (precision, threshold, len(selected))
        if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and candidate[2] > best[2]) or (candidate[0] == best[0] and candidate[2] == best[2] and candidate[1] > best[1]):
            best = candidate
    return best[1] if best else None


def _evaluation(samples: Sequence[tuple[float, int]], *, threshold: float | None, bins: Sequence[Mapping[str, Any]]) -> tuple[float | None, float | None, float | None]:
    if not samples or threshold is None:
        return None, None, None
    selected = [(s, y) for s, y in samples if s >= threshold]
    positives = sum(y for _, y in samples)
    tp = sum(y for _, y in selected)
    precision = tp / len(selected) if selected else 0.0
    recall = tp / positives if positives else 0.0
    brier = sum((_probability_from_bins(bins, s) - y) ** 2 for s, y in samples) / len(samples)
    return precision, recall, brier


def _calibration_from_row(row: Mapping[str, Any]) -> CalibrationResult:
    data = _as_mapping(row.get("calibration_json"))
    bins_raw = data.get("bins") if isinstance(data.get("bins"), list) else []
    bins = tuple(dict(x) for x in bins_raw if isinstance(x, Mapping))
    return CalibrationResult(
        calibration_id=str(row["calibration_id"]), person_id=str(row["person_id"]),
        embedding_revision=str(row["embedding_revision"]), embedding_model=str(row["embedding_model"]),
        status=str(row["status"]), threshold=float(row["threshold"]) if row.get("threshold") is not None else None,
        validation_precision=float(row["validation_precision"]) if row.get("validation_precision") is not None else None,
        validation_recall=float(row["validation_recall"]) if row.get("validation_recall") is not None else None,
        validation_brier=float(row["validation_brier"]) if row.get("validation_brier") is not None else None,
        bins=bins, labels_digest=str(row["labels_digest"]), reason=str(row.get("error_text") or "") or None,
    )


def calibrate_predictive_similarity(
    *,
    person_id: str,
    backend: DensePredictiveBackend | None = None,
    min_samples: int | None = None,
    min_validation_precision: float | None = None,
) -> CalibrationResult:
    """Fit a calibration only from explicitly verified, chronological labels."""
    ensure_predictive_schema()
    settings = get_settings()
    owner = str(person_id or "").strip()
    if not owner:
        raise PredictiveValidationError("calibration requires person_id")
    backend = backend or get_predictive_backend()
    minimum = max(6, int(min_samples if min_samples is not None else settings.v17_calibration_min_samples))
    required_precision = float(min_validation_precision if min_validation_precision is not None else settings.v17_calibration_min_validation_precision)
    rows = _label_rows(owner)
    label_digest = _json_digest([
        {"id": r["label_id"], "label": r["label"], "anchor": r["anchor_case_id"], "similar": r["similar_case_id"], "verified_at": r["verified_at"], "revisions": [r.get("anchor_source_version"), r.get("similar_source_version")]}
        for r in rows
    ])
    with connect() as con:
        existing = con.execute(
            """SELECT * FROM v18_predictive_similarity_calibrations
               WHERE person_id=? AND embedding_revision=? AND labels_digest=?
               ORDER BY updated_at DESC LIMIT 1""",
            (owner, backend.embedding_revision, label_digest),
        ).fetchone()
    if existing:
        return _calibration_from_row(dict(existing))
    now = now_iso()
    cid = stable_id("predictive_calibration18", owner, backend.embedding_revision, label_digest)

    def persist(*, status: str, train: Sequence[tuple[float, int]], validation: Sequence[tuple[float, int]], threshold: float | None, bins: Sequence[Mapping[str, Any]], error: str | None = None) -> CalibrationResult:
        precision, recall, brier = _evaluation(validation, threshold=threshold, bins=bins)
        result = CalibrationResult(
            calibration_id=cid, person_id=owner, embedding_revision=backend.embedding_revision,
            embedding_model=backend.embedding_model, status=status, threshold=threshold,
            validation_precision=precision, validation_recall=recall, validation_brier=brier,
            bins=tuple(dict(x) for x in bins), labels_digest=label_digest, reason=error,
        )
        with connect() as con, write_transaction(con):
            con.execute(
                "UPDATE v18_predictive_similarity_calibrations SET status='superseded',updated_at=? WHERE person_id=? AND embedding_revision=? AND status='accepted'",
                (now, owner, backend.embedding_revision),
            )
            con.execute(
                """INSERT INTO v18_predictive_similarity_calibrations(
                      calibration_id,person_id,embedding_revision,embedding_model,calibration_schema_revision,labels_digest,
                      train_start_at,train_end_at,validation_start_at,validation_end_at,train_samples,validation_samples,
                      train_positive,validation_positive,threshold,validation_precision,validation_recall,validation_brier,
                      status,calibration_json,error_text,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(person_id,embedding_revision,labels_digest) DO UPDATE SET
                      embedding_model=excluded.embedding_model,train_start_at=excluded.train_start_at,train_end_at=excluded.train_end_at,
                      validation_start_at=excluded.validation_start_at,validation_end_at=excluded.validation_end_at,
                      train_samples=excluded.train_samples,validation_samples=excluded.validation_samples,
                      train_positive=excluded.train_positive,validation_positive=excluded.validation_positive,
                      threshold=excluded.threshold,validation_precision=excluded.validation_precision,
                      validation_recall=excluded.validation_recall,validation_brier=excluded.validation_brier,
                      status=excluded.status,calibration_json=excluded.calibration_json,error_text=excluded.error_text,updated_at=excluded.updated_at""",
                (
                    cid, owner, backend.embedding_revision, backend.embedding_model, PREDICTIVE_SCHEMA_REVISION, label_digest,
                    # The chronological ranges are taken from labels below via closure values.
                    train_times[0] if train_times else None, train_times[-1] if train_times else None,
                    validation_times[0] if validation_times else None, validation_times[-1] if validation_times else None,
                    len(train), len(validation), sum(y for _, y in train), sum(y for _, y in validation),
                    threshold, precision, recall, brier, status,
                    json_dumps({"bins": [dict(x) for x in bins], "minimum_samples": minimum, "required_validation_precision": required_precision, "schema": PREDICTIVE_SCHEMA_REVISION}),
                    error[:2000] if error else None, now, now,
                ),
            )
        return result

    if len(rows) < minimum:
        train_times: list[str] = []
        validation_times: list[str] = []
        return persist(status="insufficient_data", train=[], validation=[], threshold=None, bins=[], error=f"need {minimum} verified causal labels, found {len(rows)}")

    # Explicit chronological split: labels are ordered by the anchor event time;
    # nothing from the future is used to choose the threshold for validation.
    cut = max(1, min(len(rows) - 1, int(math.floor(len(rows) * 0.70))))
    train_rows, validation_rows = rows[:cut], rows[cut:]
    train_times = [str(r["anchor_observed_at"]) for r in train_rows]
    validation_times = [str(r["anchor_observed_at"]) for r in validation_rows]
    if not validation_rows or len(train_rows) < max(3, minimum // 2):
        return persist(status="insufficient_data", train=[], validation=[], threshold=None, bins=[], error="chronological split leaves insufficient train or validation labels")

    def pair_case(row: Mapping[str, Any], side: str) -> dict[str, Any]:
        return {
            "person_id": owner,
            "observed_case_id": row[f"{side}_case_id"],
            "observed_at": row[f"{side}_observed_at"],
            "source_version": row[f"{side}_source_version"],
            "embedding_text": row[f"{side}_embedding_text"],
            "case_type": row.get(f"{side}_case_type"),
        }

    def score(rows_in: Sequence[Mapping[str, Any]]) -> list[tuple[float, int]]:
        samples: list[tuple[float, int]] = []
        for row in rows_in:
            anchor = pair_case(row, "anchor")
            similar = pair_case(row, "similar")
            # `score_pair` is dense + cross-encoder only. Outcome text never
            # reaches it; outcome verification exists solely in label evidence.
            _, rerank = backend.score_pair(anchor, similar)
            samples.append((rerank, int(row["label"])))
        return samples

    try:
        train = score(train_rows)
        validation = score(validation_rows)
    except PredictiveRetrievalUnavailable as exc:
        return persist(status="failed", train=[], validation=[], threshold=None, bins=[], error=str(exc))

    if len({y for _, y in train}) < 2:
        return persist(status="insufficient_data", train=train, validation=validation, threshold=None, bins=[], error="training labels need both positive and negative verified examples")
    bins = _build_bins(train)
    threshold = _best_threshold(train, min_support=max(3, minimum // 10))
    precision, _, _ = _evaluation(validation, threshold=threshold, bins=bins)
    status = "accepted" if threshold is not None and precision is not None and precision >= required_precision else "rejected"
    why = None if status == "accepted" else f"validation precision {precision!r} below required {required_precision:.3f} or no supported threshold"
    return persist(status=status, train=train, validation=validation, threshold=threshold, bins=bins, error=why)


def current_calibration(*, person_id: str, embedding_revision: str) -> CalibrationResult | None:
    ensure_predictive_schema()
    with connect() as con:
        row = con.execute(
            """SELECT * FROM v18_predictive_similarity_calibrations
               WHERE person_id=? AND embedding_revision=? AND status='accepted'
               ORDER BY updated_at DESC LIMIT 1""",
            (person_id, embedding_revision),
        ).fetchone()
    return _calibration_from_row(dict(row)) if row else None
