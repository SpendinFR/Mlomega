from __future__ import annotations

"""PredictiveRetrievalLive — dense retrieval of similar past cases in the live
loop (E34 §3).

The core ``v18_predictive_retrieval`` engine is built for the nightly calibration
pass: ``get_predictive_backend().retrieve(anchor, canonical_candidates=…)`` takes
a fully-formed *observed case* as the anchor and a candidate map. In live we have
no observed case for the current instant — only a subject string (the conversation
topic + entities in scene). This adapter bridges that gap:

* it builds a **live anchor** (a synthetic pre-outcome case whose ``embedding_text``
  is the current subject) so the backend embeds the right query;
* it loads the person's own past observed cases from ``brain2_observed_cases_v17``
  as the ``canonical_candidates`` map (the cases the backend can return);
* it calls the **real** backend ``retrieve`` and returns compact
  ``{text, score}`` rows for the "expériences similaires" hot-context section.

Everything degrades cleanly: if Qdrant / the reranker / the cases table is
unavailable, :meth:`retrieve_for_live` returns ``[]`` (the scene adapter logs one
WARN). It never blocks and never raises.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class PredictiveRetrievalLive:
    """Live wrapper around the dense predictive backend. Injectable backend so the
    Qdrant frontier can be mocked in tests (the core engine stays untouched)."""

    def __init__(self, *, backend: Any = None, db_path: Any = None, candidate_limit: int = 40) -> None:
        self._backend = backend
        self.db_path = db_path
        self.candidate_limit = int(candidate_limit)
        self.metrics = {"retrievals": 0, "hits": 0, "unavailable": 0}

    def _get_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        from mlomega_audio_elite.v18_predictive_retrieval import get_predictive_backend  # type: ignore

        self._backend = get_predictive_backend()
        return self._backend

    def _live_anchor(self, *, person_id: str, query_text: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "person_id": person_id,
            "observed_case_id": f"live-anchor:{person_id}",
            "observed_at": now,
            "created_at": now,
            "source_version": "v19-live",
            "embedding_text": query_text,
        }

    def _canonical_candidates(self, *, person_id: str) -> dict[str, dict[str, Any]]:
        """The person's past observed cases, keyed by observed_case_id."""
        try:
            from mlomega_audio_elite.db import connect  # type: ignore

            with connect(self.db_path) as con:
                rows = [
                    dict(r)
                    for r in con.execute(
                        """SELECT * FROM brain2_observed_cases_v17
                           WHERE person_id=? AND embedding_text IS NOT NULL AND embedding_text != ''
                           ORDER BY observed_at DESC LIMIT ?""",
                        (person_id, self.candidate_limit),
                    ).fetchall()
                ]
        except Exception:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            cid = str(r.get("observed_case_id") or "")
            if cid:
                out[cid] = r
        return out

    def retrieve_for_live(self, *, person_id: str, query_text: str, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return up to a few similar past cases for the current subject.

        Raises nothing: any frontier failure returns ``[]`` (honest degrade)."""
        self.metrics["retrievals"] += 1
        query_text = (query_text or "").strip()
        if not query_text:
            return []
        candidates = self._canonical_candidates(person_id=person_id)
        if not candidates:
            self.metrics["unavailable"] += 1
            return []
        try:
            backend = self._get_backend()
            anchor = self._live_anchor(person_id=person_id, query_text=query_text)
            cands = backend.retrieve(anchor, canonical_candidates=candidates, limit=3)
        except Exception as exc:
            self.metrics["unavailable"] += 1
            print(f"[predictive_live] retrieval unavailable: {str(exc)[:120]}", file=sys.stderr)
            return []
        out: list[dict[str, Any]] = []
        for c in (cands or [])[:3]:
            text = getattr(c, "predictive_text", None)
            score = getattr(c, "rerank_score", None)
            if text is None and isinstance(c, dict):
                text = c.get("text")
                score = c.get("score")
            if text:
                out.append({"text": str(text)[:200], "score": score})
        self.metrics["hits"] += len(out)
        return out
