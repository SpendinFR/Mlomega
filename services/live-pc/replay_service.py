from __future__ import annotations

"""ReplayService — end-to-end time-range replay for the glasses (E35 §2).

A ``replay`` intent (E33 « rejoue 14h30 » → routed by the NL-first router) carries
a spoken time. This service turns that time into a real *replay bundle* assembled
from the EXISTING core tables — nothing new is stored:

* **keyframes** — ``vision_frames`` rows in the window (``image_path`` per frame);
* **clips** — ``visual_evidence_assets_v19`` rows of kind clip/video in the window
  (``uri``);
* **events** — ``visual_events_v19`` rows in the window (what happened);
* **transcript** — the ``turns`` of the window (what was said), scoped to the
  owner, joined to their conversation times.

The window is derived from a spoken hour (« 14h », « 14h30 ») anchored to *today*
(or an explicit date). The bundle is then delivered two ways (both bounded):

1. a ``virtual_screen`` **UIIntent** whose content is the ordered image/clip
   **refs** (paths/URIs + a served URL base) — the Unity ``VirtualScreen`` loads a
   texture per ref; images travel as **refs, never raw bytes**, over the
   DataChannel (interdit E35: no unbounded audio/video on the channel). The web
   viewer sequences the same refs as an <img> slideshow;
2. a **timeline ContextCard** summarising the window (counts + a few event lines).

ADR (docs/DECISIONS.md §E35) — why NOT ``v18_replay.replay_offline``: that primitive
is *conversation*-scoped (requires a ``conversation_id``), turn-only, and runs the
heavy governance/manifest assembly chain for an isolated historical *reasoning*
replay. E35 replay is *time-range visual*: keyframes + clips + events + transcript
by clock window, for on-glasses display. Different inputs (time vs conversation),
different output (image sequence vs context manifest). We reuse the turn read
model conceptually but query the real tables directly; ``v18_replay`` stays for the
offline reasoning path it was built for.
"""

import re
import sys
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# --------------------------------------------------------------------------- time
_TIME_RE = re.compile(r"(\d{1,2})\s*(?:[h:]\s*(\d{1,2})?)?", re.IGNORECASE)


def parse_time_window(
    spoken: str,
    *,
    date: str | None = None,
    window_minutes: int = 15,
    now: datetime | None = None,
) -> tuple[str, str] | None:
    """Parse « 14h30 » / « 14h » / « 14:30 » → an ISO (start, end) window.

    The window is centred loosely on the spoken minute: [t, t+window_minutes] so
    « rejoue 14h30 » returns 14:30–14:45. Anchored to ``date`` (default: today,
    UTC). Returns ``None`` when no hour can be parsed."""
    if not spoken:
        return None
    m = _TIME_RE.search(str(spoken))
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = now or datetime.now(timezone.utc)
    if date:
        try:
            base = datetime.fromisoformat(f"{date}T00:00:00+00:00")
        except ValueError:
            base = now
    else:
        base = now
    start = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = start + timedelta(minutes=max(1, int(window_minutes)))
    return start.isoformat(), end.isoformat()


# --------------------------------------------------------------------------- service
class ReplayService:
    """Assembles + delivers a time-range replay bundle. Transport-agnostic:
    ``emit_ui_intent`` pushes the virtual_screen intent + timeline card."""

    def __init__(
        self,
        *,
        person_id: str = "me",
        live_session_id: str = "live",
        db_path: Any = None,
        emit_ui_intent: Any = None,
        media_url_base: str = "/replay/media",
        max_keyframes: int = 24,
        max_clips: int = 6,
        max_events: int = 20,
        max_transcript_turns: int = 20,
        window_minutes: int = 15,
    ) -> None:
        self.person_id = person_id or "me"
        self.live_session_id = live_session_id
        self.db_path = db_path
        self._emit = emit_ui_intent
        self.media_url_base = media_url_base.rstrip("/")
        self.max_keyframes = int(max_keyframes)
        self.max_clips = int(max_clips)
        self.max_events = int(max_events)
        self.max_transcript_turns = int(max_transcript_turns)
        self.window_minutes = int(window_minutes)
        self.metrics = {
            "replays": 0, "keyframes": 0, "clips": 0, "events": 0,
            "transcript_turns": 0, "empty_windows": 0, "parse_failures": 0,
        }

    # ------------------------------------------------------------------ assemble
    def assemble_bundle(
        self, *, start: str, end: str
    ) -> dict[str, Any]:
        """Read the real tables in the [start, end] window → a replay bundle."""
        keyframes = self._keyframes(start, end)
        clips = self._clips(start, end)
        events = self._events(start, end)
        transcript = self._transcript(start, end)
        self.metrics["keyframes"] += len(keyframes)
        self.metrics["clips"] += len(clips)
        self.metrics["events"] += len(events)
        self.metrics["transcript_turns"] += len(transcript)
        if not (keyframes or clips or events or transcript):
            self.metrics["empty_windows"] += 1
        return {
            "type": "replay_bundle",
            "person_id": self.person_id,
            "window": {"start": start, "end": end},
            "keyframes": keyframes,
            "clips": clips,
            "events": events,
            "transcript": transcript,
            "counts": {
                "keyframes": len(keyframes), "clips": len(clips),
                "events": len(events), "transcript_turns": len(transcript),
            },
        }

    def _connect(self):
        from mlomega_audio_elite.db import connect  # type: ignore

        return connect(self.db_path)

    def _keyframes(self, start: str, end: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as con:
                rows = [dict(r) for r in con.execute(
                    """SELECT frame_id, image_path, image_sha256, captured_at
                       FROM vision_frames
                       WHERE captured_at >= ? AND captured_at <= ?
                       ORDER BY captured_at LIMIT ?""",
                    (start, end, self.max_keyframes),
                ).fetchall()]
        except Exception:
            return []
        return [{
            "frame_id": r.get("frame_id"),
            "path": r.get("image_path"),
            "sha256": r.get("image_sha256"),
            "captured_at": r.get("captured_at"),
            "url": self._media_url("frame", r.get("frame_id")),
        } for r in rows if r.get("image_path")]

    def _clips(self, start: str, end: str) -> list[dict[str, Any]]:
        try:
            from mlomega_audio_elite.v19_visual_store import ensure_v19_visual_schema  # type: ignore

            ensure_v19_visual_schema(self.db_path)
        except Exception:
            pass
        try:
            with self._connect() as con:
                rows = [dict(r) for r in con.execute(
                    """SELECT visual_asset_id, asset_kind, uri, sha256, captured_at
                       FROM visual_evidence_assets_v19
                       WHERE person_id=? AND captured_at >= ? AND captured_at <= ?
                         AND asset_kind IN ('clip','video','gif')
                       ORDER BY captured_at LIMIT ?""",
                    (self.person_id, start, end, self.max_clips),
                ).fetchall()]
        except Exception:
            return []
        return [{
            "asset_id": r.get("visual_asset_id"),
            "kind": r.get("asset_kind"),
            "uri": r.get("uri"),
            "captured_at": r.get("captured_at"),
            "url": self._media_url("clip", r.get("visual_asset_id")),
        } for r in rows]

    def _events(self, start: str, end: str) -> list[dict[str, Any]]:
        try:
            from mlomega_audio_elite.v19_visual_store import ensure_v19_visual_schema  # type: ignore
            from mlomega_audio_elite.utils import json_loads  # type: ignore

            ensure_v19_visual_schema(self.db_path)
        except Exception:
            return []
        try:
            with self._connect() as con:
                rows = [dict(r) for r in con.execute(
                    """SELECT visual_event_id, event_type, occurred_at, entity_json
                       FROM visual_events_v19
                       WHERE person_id=? AND occurred_at >= ? AND occurred_at <= ?
                       ORDER BY occurred_at LIMIT ?""",
                    (self.person_id, start, end, self.max_events),
                ).fetchall()]
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            entity = json_loads(r.get("entity_json"), {}) or {}
            out.append({
                "event_id": r.get("visual_event_id"),
                "event_type": r.get("event_type"),
                "occurred_at": r.get("occurred_at"),
                "label": entity.get("label") if isinstance(entity, Mapping) else None,
            })
        return out

    def _transcript(self, start: str, end: str) -> list[dict[str, Any]]:
        """Turns spoken in the window, owner-scoped. The ``turns`` schema stores a
        per-turn offset (``start_s``) from the conversation ``started_at``; we
        reconstruct each turn's absolute time and filter to the window in Python.
        Best-effort: a fresh DB with no conversations yields []."""
        try:
            from mlomega_audio_elite.utils import parse_iso_utc  # type: ignore
        except Exception:
            parse_iso_utc = None
        try:
            with self._connect() as con:
                rows = [dict(r) for r in con.execute(
                    """SELECT t.turn_id, t.text, t.speaker_label, t.start_s,
                              c.started_at AS conv_start
                       FROM turns t JOIN conversations c
                            ON c.conversation_id = t.conversation_id
                       WHERE (t.person_id=? OR t.person_id IS NULL)
                       ORDER BY c.started_at, t.idx""",
                    (self.person_id,),
                ).fetchall()]
        except Exception:
            return []
        try:
            win_start = datetime.fromisoformat(start)
            win_end = datetime.fromisoformat(end)
        except ValueError:
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            if not (r.get("text") or "").strip():
                continue
            conv_start = r.get("conv_start")
            if not conv_start:
                continue
            try:
                base = datetime.fromisoformat(str(conv_start))
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
                at = base + timedelta(seconds=float(r.get("start_s") or 0.0))
            except (ValueError, TypeError):
                continue
            if win_start <= at <= win_end:
                out.append({
                    "turn_id": r.get("turn_id"),
                    "speaker": r.get("speaker_label"),
                    "text": (r.get("text") or "")[:200],
                    "at": at.isoformat(),
                })
            if len(out) >= self.max_transcript_turns:
                break
        return out

    def _media_url(self, kind: str, asset_id: Any) -> str | None:
        if not asset_id:
            return None
        return f"{self.media_url_base}/{kind}/{asset_id}"

    # ------------------------------------------------------------------ intents
    def virtual_screen_intent(self, bundle: Mapping[str, Any]) -> dict[str, Any]:
        """A ``virtual_screen`` UIIntent: ordered image/clip refs for the Unity
        VirtualScreen texture sequence (refs only, bounded count)."""
        frames = [
            {"ref": k.get("url") or k.get("path"), "path": k.get("path"),
             "at": k.get("captured_at"), "frame_id": k.get("frame_id")}
            for k in (bundle.get("keyframes") or [])
        ]
        clips = [
            {"ref": c.get("url") or c.get("uri"), "uri": c.get("uri"),
             "at": c.get("captured_at"), "asset_id": c.get("asset_id")}
            for c in (bundle.get("clips") or [])
        ]
        return {
            "type": "ui_intent",
            "ui_intent_id": str(uuid.uuid4()),
            "producer": "ultralive",
            "component": "virtual_screen",
            "content": {
                "kind": "replay",
                "window": bundle.get("window"),
                "frames": frames,
                "clips": clips,
                "counts": bundle.get("counts"),
            },
            "truth_level": "observed",
            "confidence": 1.0,
            "priority": 0.6,
            "ttl_ms": 20000,
            "evidence_refs": [k.get("path") for k in (bundle.get("keyframes") or []) if k.get("path")][:8],
        }

    def timeline_card_intent(self, bundle: Mapping[str, Any]) -> dict[str, Any]:
        """A ContextCard summarising the window (counts + a few event lines)."""
        counts = bundle.get("counts") or {}
        window = bundle.get("window") or {}
        lines: list[str] = []
        for e in (bundle.get("events") or [])[:4]:
            label = e.get("label") or e.get("event_type")
            at = str(e.get("occurred_at") or "")[11:16]
            lines.append(f"{at} {label}")
        start = str(window.get("start") or "")[11:16]
        summary = (
            f"Replay {start} — {counts.get('keyframes', 0)} images, "
            f"{counts.get('clips', 0)} clips, {counts.get('events', 0)} évènements, "
            f"{counts.get('transcript_turns', 0)} tours"
        )
        return {
            "type": "ui_intent",
            "ui_intent_id": str(uuid.uuid4()),
            "producer": "ultralive",
            "component": "context_card",
            "content": {
                "kind": "replay_timeline",
                "window": window,
                "summary": summary,
                "timeline": lines,
                "counts": counts,
            },
            "truth_level": "remembered",
            "confidence": 0.9,
            "priority": 0.5,
            "ttl_ms": 12000,
            "evidence_refs": [],
        }

    def _emit_ui(self, intent: dict[str, Any]) -> None:
        if self._emit is not None:
            try:
                self._emit(intent)
            except Exception:
                pass

    # ------------------------------------------------------------------ entry
    def replay(self, *, time: str, date: str | None = None) -> dict[str, Any]:
        """Full path: spoken time → window → bundle → virtual_screen + timeline.

        Returns the bundle plus the two emitted intents. Never raises: an
        unparseable time yields an honest ``unknown_time`` result."""
        window = parse_time_window(time, date=date, window_minutes=self.window_minutes)
        if window is None:
            self.metrics["parse_failures"] += 1
            card = {
                "type": "ui_intent", "ui_intent_id": str(uuid.uuid4()),
                "producer": "ultralive", "component": "context_card",
                "content": {"kind": "replay_unknown_time",
                            "text": f"Je n'ai pas compris l'heure : « {time} »"},
                "truth_level": "inferred", "confidence": 0.0, "priority": 0.4,
                "ttl_ms": 6000, "evidence_refs": [],
            }
            self._emit_ui(card)
            return {"status": "unknown_time", "time": time, "ui_intent": card}
        start, end = window
        bundle = self.assemble_bundle(start=start, end=end)
        vscreen = self.virtual_screen_intent(bundle)
        timeline = self.timeline_card_intent(bundle)
        self._emit_ui(vscreen)
        self._emit_ui(timeline)
        self.metrics["replays"] += 1
        return {
            "status": "ok",
            "window": bundle["window"],
            "bundle": bundle,
            "virtual_screen": vscreen,
            "timeline": timeline,
        }
