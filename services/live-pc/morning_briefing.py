from __future__ import annotations

"""MorningBriefing — one "Bonjour" card on the first session of the day (E34 §6).

At the FIRST live session of a day, the system greets the user with a single
compact ContextCard summarising what the nightly engines prepared:

* the day's predictions (short),
* the nightly proactive interventions still pending,
* the clarification questions waiting to be asked,
* the most useful last-seen objects (phone / keys …).

"First session of the day" is detected from the **real** ``brainlive_sessions``
table: if a session for this person already started earlier today, this is not the
first one and no briefing is produced. The card is enqueued via the same
``enqueue_delivery`` primitive as every other delivery, with ``source_key =
briefing:<date>`` — so even if the detection is called twice it deduplicates
naturally (dedup is keyed on person + session + source_key).

Never raises: a cold DB / missing engine yields ``{"status": "skipped"}``.
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


class MorningBriefing:
    """Builds and enqueues the first-session-of-the-day briefing card."""

    def __init__(
        self,
        *,
        person_id: str = "me",
        live_session_id: str,
        proactive: Any = None,
        worldbrain: Any = None,
        db_path: Any = None,
        max_last_seen: int = 3,
    ) -> None:
        self.person_id = person_id or "me"
        self.live_session_id = live_session_id
        self.proactive = proactive
        self.worldbrain = worldbrain
        self.db_path = db_path
        self.max_last_seen = int(max_last_seen)
        self.metrics = {"briefings_built": 0, "briefings_enqueued": 0, "skipped_not_first": 0}

    # ------------------------------------------------------------ first-of-day
    def is_first_session_today(self, *, package_date: str | None = None) -> bool:
        """True iff no earlier ``brainlive_sessions`` row exists for today.

        The current session's own row is excluded (by live_session_id) so calling
        this after the session opens still returns True on the first session."""
        day = _today(package_date)
        try:
            from mlomega_audio_elite.brainlive_v15 import ensure_brainlive_schema  # type: ignore
            from mlomega_audio_elite.db import connect  # type: ignore

            ensure_brainlive_schema()
            with connect(self.db_path) as con:
                row = con.execute(
                    """SELECT COUNT(*) FROM brainlive_sessions
                       WHERE person_id=? AND substr(started_at,1,10)=? AND live_session_id != ?""",
                    (self.person_id, day, self.live_session_id),
                ).fetchone()
            earlier = int(row[0]) if row else 0
            return earlier == 0
        except Exception:
            # Cannot tell → be conservative and do not spam a briefing.
            return False

    # ------------------------------------------------------------ build + deliver
    def maybe_deliver(self, *, package_date: str | None = None, force: bool = False) -> dict[str, Any]:
        """Deliver the briefing if this is the first session of the day.

        Returns the enqueue result, or ``{"status": "skipped", ...}``. Idempotent:
        the ``briefing:<date>`` source_key dedups within the session."""
        day = _today(package_date)
        if not force and not self.is_first_session_today(package_date=day):
            self.metrics["skipped_not_first"] += 1
            return {"status": "skipped", "reason": "not_first_session_today"}
        message, evidence = self._build_card(package_date=day)
        self.metrics["briefings_built"] += 1
        result = self._enqueue(source_key=f"briefing:{day}", message=message, evidence_refs=evidence)
        if result.get("status") == "queued":
            self.metrics["briefings_enqueued"] += 1
        return result

    def _build_card(self, *, package_date: str) -> tuple[str, list[str]]:
        preds: list[dict[str, Any]] = []
        intervs: list[dict[str, Any]] = []
        clars: list[dict[str, Any]] = []
        evidence: list[str] = []
        if self.proactive is not None:
            try:
                self.proactive.refresh(package_date=package_date)
                snap = self.proactive.snapshot()
                preds = snap.get("predictions") or []
                intervs = snap.get("interventions") or []
                clars = snap.get("clarifications") or []
                for p in self.proactive.predictions:
                    evidence.extend(p.get("evidence_refs") or [])
            except Exception:
                pass
        last_seen = self._top_last_seen()

        lines: list[str] = ["Bonjour — aujourd'hui :"]
        for p in preds[:3]:
            stmt = str(p.get("statement") or "").strip()
            if stmt:
                lines.append(f"• {stmt}")
        for i in intervs[:2]:
            t = str(i.get("message") or i.get("title") or "").strip()
            if t:
                lines.append(f"• {t}")
        for c in clars[:2]:
            q = str(c.get("question") or "").strip()
            if q:
                lines.append(f"• (à confirmer) {q}")
        if last_seen:
            lines.append("• Vu récemment : " + ", ".join(last_seen))
        if len(lines) == 1:
            lines.append("• Rien de particulier prévu.")
        return "\n".join(lines), list(dict.fromkeys(evidence))[:20]

    def _top_last_seen(self) -> list[str]:
        """Most useful last-seen object labels (phone / keys …) from the WorldBrain."""
        if self.worldbrain is None:
            return []
        try:
            snap = self.worldbrain.snapshot()
        except Exception:
            return []
        useful_kw = ("phone", "téléphone", "telephone", "key", "clé", "cle", "wallet",
                     "portefeuille", "sac", "bag", "glasses", "lunettes")
        entities = [e for e in (snap.get("entities") or []) if e.get("lifecycle") == "last_seen"]
        entities.sort(key=lambda e: e.get("age_seconds", 1e9))
        out: list[str] = []
        for e in entities:
            label = str(e.get("label") or "").lower()
            if any(k in label for k in useful_kw):
                out.append(str(e.get("label")))
            if len(out) >= self.max_last_seen:
                break
        # fall back to the freshest last-seen if none matched the "useful" list
        if not out:
            out = [str(e.get("label")) for e in entities[: self.max_last_seen] if e.get("label")]
        return out

    def _enqueue(self, *, source_key: str, message: str, evidence_refs: Sequence[str]) -> dict[str, Any]:
        try:
            from mlomega_audio_elite import v18_delivery, v19_visual_context  # type: ignore

            # Ensure a brainlive_sessions row so enqueue_delivery resolves the owner.
            try:
                v19_visual_context.publish_visual_context(
                    person_id=self.person_id, live_session_id=self.live_session_id,
                    world_state=None, observations=None, db_path=self.db_path,
                )
            except Exception:
                pass
            candidate = {
                "message": message,
                "decision": "notify",
                "action_type": "context_card",
                "kind": "morning_briefing",
                "cooldown_key": source_key,
                "candidate_id": source_key,
                "priority": 0.7,
                "evidence_refs": list(evidence_refs),
            }
            return v18_delivery.enqueue_delivery(
                live_session_id=self.live_session_id,
                source_key=source_key,
                candidate=candidate,
            )
        except Exception as exc:
            print(f"[morning_briefing] enqueue failed (non-fatal): {str(exc)[:150]}", file=sys.stderr)
            return {"status": "error", "error": str(exc)[:150]}
