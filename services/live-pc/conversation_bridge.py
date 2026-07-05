from __future__ import annotations

"""ConversationBridge — feeds V19 live transcripts into the V18.8 BrainLive
conversational loop (E31, the priority branch of PROD_BACKLOG).

The V18.8 core already contains the whole conversational reactivity engine — the
turn buffer, the adaptive dispatch policy (``v18_8_live_policy.plan_live_dispatch``),
the hot capsule / relation packs / open loops, the H1 hot loop
(``brainlive_hotloop_v15_6``) and the delivery queue consumed by
``delivery_adapter`` (E6). What it never received was the *live conversation* of
V19: AudioRT produces subtitles (a reflex DataChannel path) but nothing wrote
those final segments into the core's turn buffer.

This module is that missing pipe — and nothing more. It:

1. opens (once) a real ``brainlive_sessions`` row via the core's official
   ``start_live_session`` and shares its ``live_session_id`` for the whole V19
   session (the "shared V19 live_session_id");
2. on every *final* AudioRT segment, ingests one turn through the core's official
   ingestion function ``brainlive_v15.ingest_live_turn`` (source-addressable, with
   correct UTC timestamps, generic ``speaker_label`` — identity arrives in E32);
3. asks the core's own debounce policy ``plan_live_dispatch(audio_content=True)``
   whether a hot cycle is due, and — when it is — runs the *existing* hot loop via
   ``optimized_hot_brainlive_cycle`` (the same call the core service uses), which
   produces H1 candidates and writes them to the delivery queue via
   ``enqueue_delivery``. The rest of the chain (queue → delivery_adapter → device)
   is untouched.

ADR (docs/DECISIONS.md §E31) records why we drive the *existing* hot loop through
a caller-owned ``tick()`` instead of launching the full file-inbox daemon
(``brainlive_service_v15_5``): the daemon watches raw media directories and owns
nightly/close-day scheduling that a live XR pipeline must not; the hot cycle
itself is the reusable unit, and ``tick()`` exposes it for ``live_pipeline`` to
call synchronously right after a transcript lands. Cadences/windows are the
core's own defaults (``MLOMEGA_BRAINLIVE_LLM_*`` env vars); XR may shorten them
via those same env vars without any default being changed here.
"""

import importlib.util
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationBridge:
    """Injects final AudioRT segments into the V18.8 conversational loop.

    One instance per live session. It lazily opens a BrainLive session bound to
    ``person_id`` on the first turn (or eagerly via :meth:`ensure_session`) and
    reuses that ``live_session_id`` for every subsequent turn so the whole V19
    conversation lands in one durable scene.
    """

    def __init__(
        self,
        *,
        person_id: str = "me",
        title: str | None = "V19 live conversation",
        location_hint: str | None = None,
        speaker_label: str = "speaker",
        run_hot_cycle: bool = True,
        on_dispatch: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.person_id = person_id or "me"
        self.title = title
        self.location_hint = location_hint
        self.speaker_label = speaker_label
        # Live XR can disable the in-line hot cycle (e.g. to run it on a separate
        # cadence) without losing turn ingestion. Default: run it, so a fresh
        # deployment is reactive out of the box.
        self.run_hot_cycle = run_hot_cycle
        self.on_dispatch = on_dispatch
        self.live_session_id: str | None = None
        self._lock = threading.Lock()
        self.metrics: dict[str, Any] = {
            "conversation_turns": 0,
            "h1_candidates": 0,
            "hot_cycles": 0,
            "dispatch_skipped": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------ session
    def ensure_session(self) -> str:
        """Open (once) the shared BrainLive live session and return its id."""
        with self._lock:
            if self.live_session_id:
                return self.live_session_id
            from mlomega_audio_elite.brainlive_v15 import (  # type: ignore
                ensure_brainlive_schema,
                start_live_session,
            )

            ensure_brainlive_schema()
            res = start_live_session(
                person_id=self.person_id,
                title=self.title,
                location_hint=self.location_hint,
                mode="live_xr",
            )
            self.live_session_id = str(res["live_session_id"])
            return self.live_session_id

    def bind_session(self, live_session_id: str) -> None:
        """Reuse an already-open BrainLive session instead of creating one."""
        with self._lock:
            self.live_session_id = str(live_session_id)

    # ------------------------------------------------------------ ingest
    def ingest_segment(
        self,
        text: str,
        *,
        language: str | None = None,
        is_final: bool = True,
        timestamp_start: str | None = None,
        timestamp_end: str | None = None,
        duration_s: float | None = None,
        speaker_label: str | None = None,
        speaker_person_id: str | None = None,
        event_id: str | None = None,
        asr_confidence: float = 0.0,
        run_hot_cycle: bool | None = None,
    ) -> dict[str, Any] | None:
        """Ingest one FINAL AudioRT segment into the core conversational loop.

        Returns ``{"live_turn_id", "live_session_id", "dispatch", "hot"}`` on a
        real turn, or ``None`` when the segment is not a usable final transcript
        (empty text or ``is_final`` false — partials never enter the buffer, they
        stay on the reflex subtitle path).
        """
        text = (text or "").strip()
        if not text or not is_final:
            return None
        session_id = self.ensure_session()

        # Timestamps: prefer the AudioRT-provided window; otherwise stamp now and
        # derive an end from the duration so the core's monotonic checks hold.
        start_dt = _parse_iso(timestamp_start) if timestamp_start else _now()
        if timestamp_end:
            end_dt = _parse_iso(timestamp_end)
        elif duration_s and duration_s > 0:
            end_dt = start_dt + timedelta(seconds=float(duration_s))
        else:
            end_dt = start_dt
        if end_dt < start_dt:
            end_dt = start_dt

        metadata: dict[str, Any] = {
            "source": "v19_audiort",
            "asr_confidence": float(asr_confidence or 0.0),
        }
        if language:
            metadata["language"] = language
        if event_id:
            # A source identity makes retries update one logical turn (§V18 turn
            # source map) instead of duplicating the utterance.
            metadata["event_id"] = str(event_id)

        from mlomega_audio_elite.brainlive_v15 import ingest_live_turn  # type: ignore

        try:
            turn = ingest_live_turn(
                session_id,
                text,
                speaker_label=speaker_label or self.speaker_label,
                # E32/E37: identity resolved live (voice match, incl. the owner enrolled
                # is_user=True); None until a match sets it.
                speaker_person_id=speaker_person_id,
                is_final=True,
                timestamp_start=_iso(start_dt),
                timestamp_end=_iso(end_dt),
                metadata=metadata,
            )
        except Exception:
            self.metrics["errors"] += 1
            raise
        self.metrics["conversation_turns"] += 1

        result: dict[str, Any] = {
            "live_turn_id": turn.get("live_turn_id"),
            "live_session_id": session_id,
            "dispatch": None,
            "hot": None,
        }

        do_cycle = self.run_hot_cycle if run_hot_cycle is None else bool(run_hot_cycle)
        if do_cycle:
            result.update(self.tick(audio_content=True))
        return result

    # ------------------------------------------------------------ tick
    def tick(self, *, audio_content: bool = True, force_context: bool = False) -> dict[str, Any]:
        """Ask the core debounce policy, and run the existing hot cycle if due.

        This is the *only* place the V18.8 conversational engine is driven; it is
        the caller-owned equivalent of one ``service_iteration`` dispatch step,
        with no file-inbox / nightly ownership (ADR §E31). Idempotent and safe to
        call on a cadence even when no new segment arrived (``audio_content`` then
        False just refreshes context).
        """
        session_id = self.ensure_session()
        from mlomega_audio_elite.v18_8_live_policy import (  # type: ignore
            mark_live_dispatch,
            plan_live_dispatch,
        )

        plan = plan_live_dispatch(
            live_session_id=session_id,
            audio_content=bool(audio_content),
            cadence_due=False,
            silence_boundary=False,
            audio_observed=True,
        )
        out: dict[str, Any] = {"dispatch": plan, "hot": None}
        if self.on_dispatch is not None:
            try:
                self.on_dispatch(plan)
            except Exception:
                pass
        if not plan.get("should_dispatch_llm"):
            self.metrics["dispatch_skipped"] += 1
            return out

        from mlomega_audio_elite.brainlive_invalidation_v15_7 import (  # type: ignore
            optimized_hot_brainlive_cycle,
        )

        try:
            hot = optimized_hot_brainlive_cycle(
                session_id,
                person_id=self.person_id,
                meaningful_signal=True,
                force_context=bool(force_context or plan.get("force_context")),
            )
            status = str(hot.get("status") or "ok")
            mark_live_dispatch(
                live_session_id=session_id,
                plan=plan,
                status="ok" if status not in {"error", "failed", "llm_error"} else status,
            )
            self.metrics["hot_cycles"] += 1
            out["hot"] = hot
            self.metrics["h1_candidates"] += _count_h1_candidates(hot)
        except Exception as exc:
            self.metrics["errors"] += 1
            mark_live_dispatch(live_session_id=session_id, plan=plan, status="retryable_error")
            out["hot"] = {"status": "error", "error": str(exc)[:500]}
        return out

    def end_session(self, *, notes: str | None = None) -> dict[str, Any] | None:
        if not self.live_session_id:
            return None
        from mlomega_audio_elite.brainlive_v15 import end_live_session  # type: ignore

        try:
            return end_live_session(self.live_session_id, notes=notes)
        except Exception:
            return None


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _count_h1_candidates(hot: dict[str, Any]) -> int:
    """Count H1 candidates the hot cycle produced (queued proactive delivery)."""
    if not isinstance(hot, dict):
        return 0
    prediction = hot.get("prediction") if isinstance(hot.get("prediction"), dict) else hot
    if not isinstance(prediction, dict):
        return 0
    n = 0
    delivery_ids = prediction.get("delivery_ids")
    if isinstance(delivery_ids, list):
        n += len(delivery_ids)
    proactive = prediction.get("proactive_decision")
    if isinstance(proactive, dict) and str(proactive.get("decision") or "") in {"speak_now", "queue"} and not delivery_ids:
        n += 1
    return n


# Loadable both as a package module and via importlib (live_pipeline pattern).
def _load_sibling(name: str, filename: str):  # pragma: no cover - parity helper
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
