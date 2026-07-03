from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mlomega_audio_elite.db import connect, write_transaction
from mlomega_audio_elite.utils import now_iso
from mlomega_audio_elite.v18_predictive_retrieval import (
    DenseCandidate,
    PredictiveRetrievalUnavailable,
    register_verified_similarity_label,
)


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")
    monkeypatch.setenv("MLOMEGA_V17_CALIBRATION_MIN_SAMPLES", "6")
    monkeypatch.setenv("MLOMEGA_V17_CALIBRATION_MIN_VALIDATION_PRECISION", "0.60")


class _FakeDenseBackend:
    """Test double for a real Qdrant/embedder/reranker chain.

    It intentionally exposes only the production backend interface.  It does
    not expose or use lexical token overlap, so the test would fail if RC5
    accidentally routed back through the legacy `_case_similarity` function.
    """

    embedding_revision = "unit-dense-v1"
    embedding_model = "unit-embedding"
    collection = "existing-qdrant-v17-cases"
    rerank_candidate_limit = 20

    def __init__(self):
        self.synced: list[dict[str, Any]] = []

    def sync_cases(self, cases, *, person_id=None):
        self.synced = [dict(c) for c in cases]
        return {"indexed": len(self.synced), "skipped_unchanged": 0, "collection": self.collection}

    @staticmethod
    def _score(anchor: dict[str, Any]) -> float:
        # Explicit score supplied by the dense/cross-encoder test fixture.
        return 0.92 if str(anchor["observed_case_id"]).startswith("pos_") else 0.08

    def score_pair(self, anchor, similar):
        score = self._score(dict(anchor))
        return (0.91 if score > 0.5 else 0.09, score)

    def retrieve(self, anchor, *, canonical_candidates, limit):
        anchor = dict(anchor)
        at = datetime.fromisoformat(str(anchor["observed_at"]).replace("Z", "+00:00"))
        out = []
        for candidate in canonical_candidates.values():
            candidate = dict(candidate)
            if candidate["observed_case_id"] == anchor["observed_case_id"]:
                continue
            ct = datetime.fromisoformat(str(candidate["observed_at"]).replace("Z", "+00:00"))
            # This fake emulates Qdrant payload filtering and the mandatory
            # local defense: no other owner and no equal/future case.
            if candidate["person_id"] != anchor["person_id"] or ct >= at:
                continue
            if candidate["observed_case_id"] != "past_target":
                continue
            score = self._score(anchor)
            out.append(DenseCandidate(
                observed_case_id=candidate["observed_case_id"],
                dense_similarity=0.91,
                rerank_score=score,
                predictive_text=candidate["embedding_text"],
                payload={
                    "entity_kind": "v17_predictive_observed_case",
                    "person_id": candidate["person_id"],
                    "observed_case_id": candidate["observed_case_id"],
                    "source_version": candidate["source_version"],
                    "observed_at": candidate["observed_at"],
                    "active": True,
                    "embedding_revision": self.embedding_revision,
                },
            ))
        return out[:limit]


def _insert_case(con, *, case_id: str, at: datetime, text: str, owner: str = "me"):
    con.execute(
        """INSERT INTO brain2_observed_cases_v17(
             observed_case_id,person_id,case_type,case_key,title,context_summary,people_json,tags_json,
             comparable_vector_json,embedding_text,quality_score,confidence,observed_at,status,created_at,
             updated_at,source_version,invalidated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
        (
            case_id, owner, "work", "work", case_id, text, "[]", "[\"focus\"]", "{}", text,
            0.8, 0.8, at.isoformat(), "active", now_iso(), now_iso(), f"src-{case_id}",
        ),
    )


def _seed_calibration_cases(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import ensure_longitudinal_case_schema

    ensure_longitudinal_case_schema()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with connect() as con, write_transaction(con):
        # Ten causal pairs create a 70/30 chronological train/validation split.
        # Pos anchors receive high dense/reranker scores and label 1; neg anchors
        # receive low scores and label 0. Labels are explicitly registered after
        # all case events, never inferred from outcome token overlap.
        for i in range(10):
            similar = f"similar_{i}"
            anchor = f"{'pos' if i % 2 == 0 else 'neg'}_anchor_{i}"
            _insert_case(con, case_id=similar, at=base + timedelta(hours=i * 2), text=f"context source {i}")
            _insert_case(con, case_id=anchor, at=base + timedelta(hours=i * 2 + 1), text=f"context anchor {i}")
        _insert_case(con, case_id="past_target", at=base + timedelta(hours=30), text="target past")
        _insert_case(con, case_id="pos_target", at=base + timedelta(hours=31), text="target anchor")
        _insert_case(con, case_id="future_target", at=base + timedelta(hours=32), text="future must be excluded")
        _insert_case(con, case_id="bob_target", at=base + timedelta(hours=29), text="other owner", owner="bob")
    for i in range(10):
        anchor = f"{'pos' if i % 2 == 0 else 'neg'}_anchor_{i}"
        register_verified_similarity_label(
            person_id="me", anchor_case_id=anchor, similar_case_id=f"similar_{i}", label=(i % 2 == 0),
            label_source="human_verified", verified_at=(base + timedelta(hours=i * 2 + 1, minutes=30)).isoformat(),
            notes="explicit review",
        )


def test_rc5_dense_qdrant_path_is_causal_calibrated_and_not_lexical(monkeypatch, tmp_path):
    _seed_calibration_cases(monkeypatch, tmp_path)
    import mlomega_audio_elite.v18_longitudinal as longitudinal
    import mlomega_audio_elite.brain2_longitudinal_cases_v17 as v17

    backend = _FakeDenseBackend()
    monkeypatch.setattr(longitudinal, "get_predictive_backend", lambda: backend)
    # A legacy code path calling Jaccard must fail this test immediately.
    monkeypatch.setattr(v17, "_jaccard", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("Jaccard must not run in RC5 predictive mode")))

    result = v17.compute_global_case_similarities(
        person_id="me", anchor_case_ids=["pos_target"], top_k=10, mode="predictive"
    )
    assert result["status"] == "ok"
    assert result["edges_upserted"] == 1
    assert result["calibration"]["status"] == "accepted"
    assert result["calibration"]["validation_precision"] >= 0.60
    assert backend.synced

    with connect() as con:
        edges = [dict(r) for r in con.execute(
            """SELECT anchor_case_id,similar_case_id,final_score,dense_similarity,rerank_score,
                      calibrated_probability,outcome_similarity,retrieval_backend,embedding_revision
               FROM brain2_case_similarity_edges_v17 WHERE status='active'"""
        ).fetchall()]
    assert len(edges) == 1
    edge = edges[0]
    assert edge["anchor_case_id"] == "pos_target"
    assert edge["similar_case_id"] == "past_target"
    assert edge["outcome_similarity"] == 0.0
    assert edge["dense_similarity"] == 0.91
    assert edge["rerank_score"] == 0.92
    assert edge["final_score"] == edge["calibrated_probability"]
    assert 0 < edge["calibrated_probability"] < 1
    assert edge["retrieval_backend"] == "qdrant+dense+cross_encoder"
    assert edge["embedding_revision"] == "unit-dense-v1"


def test_rc5_without_accepted_calibration_invalidates_old_predictive_edges(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import compute_global_case_similarities, ensure_longitudinal_case_schema
    import mlomega_audio_elite.v18_longitudinal as longitudinal

    ensure_longitudinal_case_schema()
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with connect() as con, write_transaction(con):
        _insert_case(con, case_id="past", at=base, text="past")
        _insert_case(con, case_id="anchor", at=base + timedelta(hours=1), text="anchor")
        con.execute(
            """INSERT INTO brain2_case_similarity_edges_v17(
                 edge_id,person_id,anchor_case_id,similar_case_id,final_score,created_at,updated_at,
                 similarity_mode,status
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("legacy", "me", "anchor", "past", 0.9, now_iso(), now_iso(), "predictive", "active"),
        )
    monkeypatch.setattr(longitudinal, "get_predictive_backend", lambda: _FakeDenseBackend())
    result = compute_global_case_similarities(person_id="me", anchor_case_ids=["anchor"], mode="predictive")
    assert result["status"] == "abstained"
    assert result["calibration"]["status"] == "insufficient_data"
    with connect() as con:
        status = con.execute("SELECT status FROM brain2_case_similarity_edges_v17 WHERE edge_id='legacy'").fetchone()["status"]
    assert status == "invalidated"


def test_rc5_rejects_future_or_cross_owner_verified_label(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import ensure_longitudinal_case_schema

    ensure_longitudinal_case_schema()
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with connect() as con, write_transaction(con):
        _insert_case(con, case_id="older", at=base, text="older")
        _insert_case(con, case_id="newer", at=base + timedelta(hours=1), text="newer")
        _insert_case(con, case_id="bob", at=base, text="bob", owner="bob")
    with pytest.raises(ValueError, match="strictly earlier"):
        register_verified_similarity_label(
            person_id="me", anchor_case_id="older", similar_case_id="newer", label=True,
            label_source="human_verified", verified_at=(base + timedelta(hours=2)).isoformat(),
        )
    with pytest.raises(ValueError, match="belong to requested owner"):
        register_verified_similarity_label(
            person_id="me", anchor_case_id="newer", similar_case_id="bob", label=True,
            label_source="human_verified", verified_at=(base + timedelta(hours=2)).isoformat(),
        )
