from __future__ import annotations

"""V19 live PC video ingress primitives.

The gateway contract is intentionally small: producers expose an async iterator
of ``(frame_bgr, FrameEnvelope)`` and the live stack keeps only the latest frame.
This prevents latent XR backlogs from turning into stale UI overlays.
"""

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from packages.contracts.python.models import FrameEnvelope

FramePacket = tuple[Any, FrameEnvelope]


class VideoIngress(Protocol):
    def __aiter__(self) -> AsyncIterator[FramePacket]: ...


@dataclass
class DecodeBench:
    samples_ms: list[float] = field(default_factory=list)

    def add_ns(self, elapsed_ns: int) -> None:
        self.samples_ms.append(elapsed_ns / 1_000_000)

    def summary(self) -> dict[str, float | int]:
        if not self.samples_ms:
            return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0}
        ordered = sorted(self.samples_ms)
        def pct(p: float) -> float:
            idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p)))
            return ordered[idx]
        return {"count": len(ordered), "p50_ms": statistics.median(ordered), "p95_ms": pct(0.95)}


class LatestFrameQueue:
    """Single-slot async frame queue with drop accounting (queue = 1)."""

    def __init__(self) -> None:
        self._latest: FramePacket | None = None
        self._event = asyncio.Event()
        self.dropped_frames = 0
        self.received_frames = 0

    def put_latest(self, packet: FramePacket) -> None:
        if self._latest is not None:
            self.dropped_frames += 1
        self._latest = packet
        self.received_frames += 1
        self._event.set()

    async def get_latest(self) -> FramePacket:
        await self._event.wait()
        assert self._latest is not None
        packet = self._latest
        self._latest = None
        self._event.clear()
        return packet

    def stats(self) -> dict[str, int]:
        return {"received_frames": self.received_frames, "dropped_frames": self.dropped_frames, "queue_size": 1 if self._latest else 0}


class AiortcIngress:
    """Adapter wrapper for aiortc/PyAV sources.

    ``source`` may be any async iterator yielding either ``(frame, envelope)`` or
    objects with ``to_ndarray(format='bgr24')`` plus an ``envelope`` attribute.
    The concrete WebRTC server can be swapped for GStreamer without changing
    downstream consumers.
    """

    def __init__(self, source: AsyncIterator[Any]) -> None:
        self.source = source
        self.bench = DecodeBench()

    def __aiter__(self) -> AsyncIterator[FramePacket]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[FramePacket]:
        async for item in self.source:
            start = time.perf_counter_ns()
            if isinstance(item, tuple) and len(item) == 2:
                frame_bgr, envelope = item
            else:
                frame_bgr = item.to_ndarray(format="bgr24")
                envelope = item.envelope
            if not isinstance(envelope, FrameEnvelope):
                envelope = FrameEnvelope.model_validate(envelope)
            self.bench.add_ns(time.perf_counter_ns() - start)
            yield frame_bgr, envelope


async def pump_latest(ingress: VideoIngress, queue: LatestFrameQueue, *, limit: int | None = None) -> dict[str, Any]:
    count = 0
    async for packet in ingress:
        queue.put_latest(packet)
        count += 1
        if limit is not None and count >= limit:
            break
    return queue.stats()
