from __future__ import annotations

"""ProactiveContext — brings the nightly engines into the live session (E34 §2).

The V19 core runs three nightly/offline engines whose output never reached the
live glasses before E34:

* **daily predictions** (``v19_prediction_loop`` → ``predictions_v19``, status
  ``open``, each with a machine-verifiable ``verification_spec``);
* **proactive interventions** (``proactive_interventions_v14_7`` → the
  ``v14_7_intervention_queue``, pending items generated at night);
* **clarification questions** (``clarification_inbox_v14_8`` →
  ``v14_8_clarification_items``, questions the system wants to ask *at the right
  moment*).

This module is the *read side*: at session start and periodically it loads the
still-open items (LIGHT queries, all bounded) and exposes:

1. :meth:`snapshot` — a compact dict the scene adapter folds into the
   HotSceneContext under ``proactive`` (budget-bounded there);
2. :meth:`match_predictions` — the predictions whose ``verification_spec``
   matches the *current* scene/conversation, using the **exact same**
   ``_event_matches`` predicate the outcome watcher uses at night (a prediction
   fires live iff it would be verified by what is on screen / being said);
3. :meth:`relevant_interventions` — nightly interventions whose subject appears
   in the current scene/conversation;
4. :meth:`due_clarification` — one pending question, returned **only** when the
   context is calm (no active conversation) so we never interrupt a live turn.

It never writes to the core and never blocks: every core call is wrapped, and a
missing table / cold DB yields an empty section (honest degrade), never a crash.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _today(package_date: str | None = None) -> str:
    if package_date:
        return str(package_date)
    return datetime.now(timezone.utc).date().isoformat()


def _text_blob(*parts: Any) -> str:
    return " ".join(str(p or "") for p in parts).lower()


class ProactiveContext:
    """Live read-side of the nightly engines (predictions / interventions /
    clarifications). One instance per live session."""

    def __init__(
        self,
        *,
        person_id: str = "me",
        db_path: Any = None,
        max_predictions: int = 6,
        max_interventions: int = 5,
        max_clarifications: int = 5,
    ) -> None:
        self.person_id = person_id or "me"
        self.db_path = db_path
        self.max_predictions = int(max_predictions)
        self.max_interventions = int(max_interventions)
        self.max_clarifications = int(max_clarifications)
        # loaded caches
        self.predictions: list[dict[str, Any]] = []
        self.interventions: list[dict[str, Any]] = []
        self.clarifications: list[dict[str, Any]] = []
        self.loaded_at: str | None = None
        self.metrics: dict[str, Any] = {
            "loads": 0,
            "predictions_open": 0,
            "interventions_pending": 0,
            "clarifications_pending": 0,
            "prediction_matches": 0,
            "intervention_matches": 0,
            "clarifications_delivered": 0,
        }

    # ------------------------------------------------------------------ load
    def refresh(self, *, package_date: str | None = None) -> dict[str, Any]:
        """(Re)load the open items of the day. Cheap, idempotent, never raises."""
        self.predictions = self._load_predictions(package_date=package_date)
        self.interventions = self._load_interventions()
        self.clarifications = self._load_clarifications()
        self.loaded_at = datetime.now(timezone.utc).isoformat()
        self.metrics["loads"] += 1
        self.metrics["predictions_open"] = len(self.predictions)
        self.metrics["interventions_pending"] = len(self.interventions)
        self.metrics["clarifications_pending"] = len(self.clarifications)
        return self.snapshot()

    def _load_predictions(self, *, package_date: str | None) -> list[dict[str, Any]]:
        """Open predictions of the day with their parsed verification_spec.

        Read straight from ``predictions_v19`` (status ``open``) — the same rows
        the outcome watcher resolves — scoped to today's horizon so a live match
        is meaningful. Uses the core DB path (``MLOMEGA_DB``) via the core connect.
        """
        day = _today(package_date)
        try:
            from mlomega_audio_elite.v19_prediction_loop import ensure_prediction_schema  # type: ignore
            from mlomega_audio_elite.db import connect  # type: ignore
            from mlomega_audio_elite.utils import json_loads  # type: ignore

            ensure_prediction_schema(self.db_path)
            with connect(self.db_path) as con:
                rows = [
                    dict(r)
                    for r in con.execute(
                        """SELECT * FROM predictions_v19
                           WHERE person_id=? AND status='open'
                             AND (horizon_end IS NULL OR substr(horizon_end,1,10) >= ?)
                             AND (horizon_start IS NULL OR substr(horizon_start,1,10) <= ?)
                           ORDER BY confidence DESC, emitted_at DESC LIMIT ?""",
                        (self.person_id, day, day, self.max_predictions),
                    ).fetchall()
                ]
            out: list[dict[str, Any]] = []
            for r in rows:
                spec = json_loads(r.get("verification_spec_json"), {}) or {}
                out.append({
                    "prediction_id": r.get("prediction_id"),
                    "statement": r.get("statement"),
                    "confidence": r.get("confidence"),
                    "horizon_end": r.get("horizon_end"),
                    "verification_spec": spec if isinstance(spec, dict) else {},
                    "evidence_refs": json_loads(r.get("evidence_refs_json"), []) or [],
                })
            return out
        except Exception:
            return []

    def _load_interventions(self) -> list[dict[str, Any]]:
        """Pending nightly interventions (v14_7 queue, status ready/pending/snoozed)."""
        try:
            from mlomega_audio_elite import proactive_interventions_v14_7 as pi  # type: ignore

            res = pi.list_intervention_inbox(person_id=self.person_id, limit=self.max_interventions)
            items = res.get("items") if isinstance(res, dict) else None
            return [dict(i) for i in (items or [])][: self.max_interventions]
        except Exception:
            return []

    def _load_clarifications(self) -> list[dict[str, Any]]:
        """Pending clarification questions (v14_8, queued)."""
        try:
            from mlomega_audio_elite import clarification_inbox_v14_8 as ci  # type: ignore

            res = ci.list_clarifications(person_id=self.person_id, status="queued", limit=self.max_clarifications)
            items = res.get("items") if isinstance(res, dict) else None
            return [dict(i) for i in (items or [])][: self.max_clarifications]
        except Exception:
            return []

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict[str, Any]:
        """A compact, budget-friendly view for the HotSceneContext ``proactive`` slot."""
        return {
            "predictions": [
                {"id": p.get("prediction_id"), "statement": p.get("statement"),
                 "confidence": p.get("confidence")}
                for p in self.predictions
            ],
            "interventions": [
                {"id": i.get("queue_id"), "title": i.get("title"),
                 "message": i.get("message"), "why_now": i.get("why_now")}
                for i in self.interventions
            ],
            "clarifications": [
                {"id": c.get("item_id"), "question": c.get("question_text") or c.get("question")}
                for c in self.clarifications
            ],
        }

    # ------------------------------------------------- scene/conversation match
    @staticmethod
    def _scene_event(ctx: Mapping[str, Any]) -> dict[str, Any]:
        """Build a synthetic ``visual_events_v19``-shaped event from the live
        HotSceneContext so the core ``_event_matches`` predicate can score it.

        The core predicate reads entity/observation/place blobs; we pack the
        visible entity labels + the current transcript hint (conversation) into
        those fields, and the place hint into place_json."""
        labels = [str(e.get("label") or "") for e in (ctx.get("visible_entities") or [])]
        names = [str(p.get("name") or "") for p in (ctx.get("people_identified") or []) if p.get("name")]
        transcript = ""
        activity = ctx.get("activity")
        if isinstance(activity, Mapping):
            transcript = str(activity.get("transcript_hint") or "")
        place = ctx.get("place") or {}
        place_hint = place.get("place_hint") if isinstance(place, Mapping) else None
        return {
            "event_type": "visual_scene",
            "entity_json": " ".join(labels + names),
            "observation_json": transcript,
            "place_json": str(place_hint or ""),
            "provenance_json": "",
        }

    def match_predictions(self, ctx: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Predictions whose spec matches the current scene/conversation.

        Uses the outcome watcher's own ``_event_matches`` so a prediction fires
        live under exactly the conditions that would verify it at night."""
        if not self.predictions:
            return []
        try:
            from mlomega_audio_elite.v19_outcome_watcher import _event_matches  # type: ignore
        except Exception:
            return []
        event = self._scene_event(ctx)
        out: list[dict[str, Any]] = []
        for p in self.predictions:
            spec = p.get("verification_spec") or {}
            if not isinstance(spec, Mapping) or not spec:
                continue
            try:
                if _event_matches(event, spec):
                    out.append(p)
            except Exception:
                continue
        self.metrics["prediction_matches"] += len(out)
        return out

    def relevant_interventions(self, ctx: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Nightly interventions whose subject appears in the current context.

        A light lexical match: the intervention's linked person / title tokens
        against the visible labels + names + transcript. Interventions with no
        obvious subject are treated as context-free and returned as-is (they were
        already vetted at night); those with a subject only fire when it appears."""
        if not self.interventions:
            return []
        blob = _text_blob(
            *[e.get("label") for e in (ctx.get("visible_entities") or [])],
            *[p.get("name") for p in (ctx.get("people_identified") or [])],
            (ctx.get("activity") or {}).get("transcript_hint") if isinstance(ctx.get("activity"), Mapping) else "",
            (ctx.get("place") or {}).get("place_hint") if isinstance(ctx.get("place"), Mapping) else "",
        )
        out: list[dict[str, Any]] = []
        for i in self.interventions:
            subject = _text_blob(i.get("linked_person_hint") if isinstance(i, Mapping) else None).strip()
            if not subject:
                # no explicit subject hint → check title tokens for any overlap
                title_tokens = [t for t in _text_blob(i.get("title")).split() if len(t) >= 4]
                if any(t in blob for t in title_tokens):
                    out.append(i)
                continue
            if subject in blob:
                out.append(i)
        self.metrics["intervention_matches"] += len(out)
        return out

    def due_clarification(self, ctx: Mapping[str, Any], *, conversation_active: bool) -> dict[str, Any] | None:
        """One pending clarification, returned only when the context is CALM.

        We never ask the system's own question during an active conversation
        (§2c): interrupting a live turn is exactly what the clarification inbox is
        designed to avoid."""
        if conversation_active or not self.clarifications:
            return None
        return self.clarifications[0]

    def mark_clarification_delivered(self, item_id: str | None) -> None:
        """Drop a delivered clarification from the live cache (dedup within session)."""
        if not item_id:
            return
        before = len(self.clarifications)
        self.clarifications = [c for c in self.clarifications if c.get("item_id") != item_id]
        if len(self.clarifications) < before:
            self.metrics["clarifications_delivered"] += 1
