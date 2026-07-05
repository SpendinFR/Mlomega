from __future__ import annotations

"""MemoryQuery — "interroge ma mémoire" → the rich Brain2 router (E33 §2).

The vision (backlog E33 inventory add, 2026-07-04): asking your memory a real
question from the glasses must reach the *rich* Brain2 router
``brain2_router_v14_2.ask_brain2`` (natural route → SQL candidates → vector search
→ fusion/ranking → LLM answer), exactly as the CLI's ``v14-ask`` does — not the
simple ``/query`` of api.py.

This module is the thin live adapter:

* it calls the CORE ``ask_brain2(question, person_id=...)`` unchanged (no memory
  logic is reimplemented — the router owns routing/fusion/answer);
* Brain2 needs the LLM (its answer step is an LLM JSON call). When Ollama is off
  the deep path is honestly unavailable; we fall back to a simple **retrieval-only**
  answer (``retrieval.search`` — vector hits, NO LLM) when Qdrant/embeddings are
  usable, else an honest "mémoire profonde indisponible" card (ADR §E33);
* the reply is shaped as a ContextCard UIIntent with the correct ``truth_level``
  (``remembered`` for a grounded Brain2 answer, ``inferred`` for the degraded
  retrieval fallback) and ``evidence_refs`` from the answer's evidence.
"""

import sys
import uuid
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _evidence_refs(answer: dict[str, Any]) -> list[str]:
    """Pull evidence refs from a Brain2 answer packet, best-effort."""
    refs: list[str] = []
    for key in ("evidence", "citations", "sources", "fused_candidates"):
        items = answer.get(key)
        if not isinstance(items, list):
            continue
        for it in items[:8]:
            if isinstance(it, dict):
                st = it.get("source_type") or it.get("type") or "memory"
                sid = it.get("source_id") or it.get("id") or it.get("memory_id")
                if sid:
                    refs.append(f"{st}:{sid}")
            elif isinstance(it, str):
                refs.append(it)
    return refs[:8]


class MemoryQuery:
    """Route a memory question to the rich Brain2 router with an honest fallback."""

    def __init__(self, *, person_id: str = "me", limit: int = 80) -> None:
        self.person_id = person_id or "me"
        self.limit = int(limit)
        self.metrics: dict[str, Any] = {"asks": 0, "brain2_answers": 0, "retrieval_fallbacks": 0, "unavailable": 0}

    # ---- core call (kept identical to the CLI's cmd_v14_ask) ----------------
    def _ask_brain2(self, question: str) -> dict[str, Any]:
        from mlomega_audio_elite.brain2_router_v14_2 import ask_brain2  # type: ignore

        return ask_brain2(question, person_id=self.person_id)

    def _retrieval_only(self, question: str) -> dict[str, Any] | None:
        """LLM-free fallback: vector hits (no answer synthesis). ADR §E33."""
        try:
            from mlomega_audio_elite.retrieval import search  # type: ignore

            hits = search(question, limit=5, person_id=self.person_id)
        except Exception:
            return None
        if not hits:
            return None
        lines = []
        refs = []
        for h in hits:
            st = getattr(h, "source_type", "memory")
            sid = getattr(h, "source_id", "?")
            txt = (getattr(h, "text", "") or "")[:160]
            lines.append(f"• {txt}")
            refs.append(f"{st}:{sid}")
        return {"text": "\n".join(lines), "evidence_refs": refs[:6]}

    # ---- main entry ---------------------------------------------------------
    def ask(self, question: str) -> dict[str, Any]:
        """Answer a memory question → ContextCard UIIntent dict."""
        self.metrics["asks"] += 1
        question = (question or "").strip()
        answer_text: str
        truth_level: str
        evidence: list[str]
        source: str

        try:
            answer = self._ask_brain2(question)
            status = str(answer.get("status") or "ok")
            answer_text = str(answer.get("answer") or answer.get("summary") or "").strip()
            if status == "ok" and answer_text:
                self.metrics["brain2_answers"] += 1
                truth_level = "remembered"
                evidence = _evidence_refs(answer)
                source = "brain2"
            else:
                raise RuntimeError(f"brain2 status={status}")
        except Exception:
            fallback = self._retrieval_only(question)
            if fallback is not None:
                self.metrics["retrieval_fallbacks"] += 1
                answer_text = (
                    "Mémoire profonde indisponible (LLM éteint) — souvenirs les plus proches :\n"
                    + fallback["text"]
                )
                truth_level = "inferred"
                evidence = fallback["evidence_refs"]
                source = "retrieval"
            else:
                self.metrics["unavailable"] += 1
                answer_text = (
                    "Mémoire profonde indisponible : le moteur de mémoire (LLM local) est éteint. "
                    "Réessaie une fois Ollama démarré."
                )
                truth_level = "inferred"
                evidence = []
                source = "unavailable"

        intent = {
            "type": "ui_intent",
            "ui_intent_id": str(uuid.uuid4()),
            "producer": "brainlive",
            "component": "context_card",
            "content": {"kind": "memory_answer", "question": question, "text": answer_text, "source": source},
            "truth_level": truth_level,
            "confidence": 0.6 if source == "brain2" else 0.3,
            "priority": 0.5,
            "ttl_ms": 12000,
            "evidence_refs": evidence,
        }
        return intent
