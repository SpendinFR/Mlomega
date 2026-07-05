from __future__ import annotations

"""LiveDiscourse — fine discourse analysis on live turns, off the hot path (E34 §4).

E31 already feeds final AudioRT turns into the V18.8 *conversational* loop (turn
buffer → policy → hot loop). What it does NOT do is run the core's **fine
discourse analysis** — the ConversationMicroscope (speech acts / expressions /
ideas) and the ConversationDiscourse (topic threads) — on those live turns. That
analysis is what populates ``expression_signals`` / ``ideas`` / ``atomic_memories``
/ topic-thread tables the rest of the core learns from; before E34 it only ran on
the nightly batch import.

This module runs it live, but **never on the ingestion path**:

* every final turn is appended to a small in-memory buffer (O(1), non-blocking);
* on a cadence (``min_turns`` accumulated or ``min_interval_s`` elapsed) a
  **background worker** flushes the buffer through the core's own official
  ``ingest.ingest_transcript`` — the exact entry the batch import uses, which runs
  the microscope + discourse analyzers and writes to the **existing core tables**
  (no new table, no reimplemented persistence: §4 "brancher, pas reconstruire").

The worker is a daemon thread with a bounded queue; if it falls behind, oldest
flushes are dropped (a WARN), so live ingestion can never be back-pressured by the
analyzer. All core calls are wrapped — a cold DB / missing model degrades to a
no-op, never a crash.
"""

import queue
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveDiscourse:
    """Budgeted, background discourse analysis of live turns via the core pipeline."""

    def __init__(
        self,
        *,
        person_id: str = "me",
        min_turns: int = 4,
        min_interval_s: float = 30.0,
        max_pending_flushes: int = 4,
        ingest_fn: Any = None,
        start_worker: bool = True,
    ) -> None:
        self.person_id = person_id or "me"
        self.min_turns = int(min_turns)
        self.min_interval_s = float(min_interval_s)
        self._ingest_fn = ingest_fn  # injectable for tests (core boundary)
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self._q: "queue.Queue[dict[str, Any] | None]" = queue.Queue(maxsize=max_pending_flushes)
        self._worker: threading.Thread | None = None
        self._started_at = _now_iso()
        self.metrics = {"turns_seen": 0, "flushes": 0, "flush_errors": 0, "dropped": 0}
        if start_worker:
            self._ensure_worker()

    # ------------------------------------------------------------ public
    def note_turn(self, text: str, *, speaker_label: str | None = None, topic: str | None = None) -> None:
        """Append one FINAL turn. O(1), non-blocking; may trigger a background flush."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._buffer.append({"speaker": speaker_label or "speaker", "text": text})
            if topic and not getattr(self, "_topic", None):
                self._topic = topic
            self.metrics["turns_seen"] += 1
            due = self._flush_due_locked()
        if due:
            self._flush_async()

    def flush(self) -> bool:
        """Force a synchronous flush of the current buffer (tests / end of session)."""
        batch = self._take_batch()
        if not batch:
            return False
        self._do_flush(batch)
        return True

    def close(self) -> None:
        """Flush remaining turns and stop the worker."""
        self.flush()
        if self._worker is not None:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass

    # ------------------------------------------------------------ internals
    def _flush_due_locked(self) -> bool:
        import time

        if len(self._buffer) >= self.min_turns:
            return True
        if self._buffer and (time.monotonic() - self._last_flush) >= self.min_interval_s:
            return True
        return False

    def _take_batch(self) -> list[dict[str, Any]]:
        import time

        with self._lock:
            if not self._buffer:
                return []
            batch = self._buffer
            self._buffer = []
            self._last_flush = time.monotonic()
        return batch

    def _flush_async(self) -> None:
        batch = self._take_batch()
        if not batch:
            return
        self._ensure_worker()
        try:
            self._q.put_nowait({"turns": batch, "topic": getattr(self, "_topic", None)})
        except queue.Full:
            # Worker is behind — drop this flush rather than block live ingestion.
            self.metrics["dropped"] += 1
            print("[live_discourse] worker backlog: dropped a discourse flush", file=sys.stderr)

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="live-discourse", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            self._do_flush(job.get("turns") or [], topic=job.get("topic"))

    def _do_flush(self, turns: list[dict[str, Any]], *, topic: str | None = None) -> None:
        if not turns:
            return
        data = {
            "metadata": {
                "conversation_id": f"live:{self.person_id}:{self._started_at}",
                "topic": topic or "conversation live",
                "participants": [self.person_id],
                "channel": "audio/live_xr",
                "source": "v19_live_discourse",
                "started_at": self._started_at,
            },
            "turns": turns,
        }
        try:
            ingest = self._ingest_fn
            if ingest is None:
                from mlomega_audio_elite.ingest import ingest_transcript  # type: ignore

                ingest = ingest_transcript
            ingest(data)
            self.metrics["flushes"] += 1
        except Exception as exc:
            self.metrics["flush_errors"] += 1
            print(f"[live_discourse] flush failed (non-fatal): {str(exc)[:150]}", file=sys.stderr)
