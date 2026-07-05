"""Degraded-mode state machine for the V19 live PC.

Handoff §3.6 defines the degradation ladder the GpuArbiter/StatusBar must apply
under pressure:

    detector -> floor cadence (5 fps); change detection paused; VLM refused;
    never touch the tracker or subtitles.

This module turns raw signals into a ``degraded_state`` event plus a
recommended *action level* on that ladder. Inputs:

* PC/session heartbeat timestamp (``pc_absent`` when stale).
* free VRAM in MB (``gpu_pressure`` when below a floor).
* dropped frame count / network latency (``network_degraded`` when over
  thresholds).

Outputs are plain dicts (``{"type": "degraded_state", ...}``) suitable for
pushing to renderers (StatusBar). Deterministic and unit-tested; no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Action ladder (handoff §3.6). Ordered least -> most severe.
ACTION_NOMINAL = "nominal"
ACTION_DETECTOR_FLOOR = "detector_floor"       # detector dropped to 5 fps
ACTION_PAUSE_CHANGES = "pause_change_detection"  # change detection paused
ACTION_REFUSE_VLM = "refuse_vlm"               # VLM jobs refused
ACTION_PC_UNAVAILABLE = "pc_unavailable"       # reflexes on device only

_ACTION_RANK = {
    ACTION_NOMINAL: 0,
    ACTION_DETECTOR_FLOOR: 1,
    ACTION_PAUSE_CHANGES: 2,
    ACTION_REFUSE_VLM: 3,
    ACTION_PC_UNAVAILABLE: 4,
}


@dataclass
class DegradedThresholds:
    heartbeat_stale_s: float = 6.0          # session heartbeat considered dead after this
    vram_floor_mb: int = 800               # below -> gpu pressure (refuse vlm)
    vram_warn_mb: int = 1500               # below -> detector floor / pause changes
    max_frame_drops: int = 30              # drops within window before network_degraded
    max_network_latency_ms: float = 250.0  # RTT above this -> network_degraded


# --------------------------------------------------------------------------- WAN
# E36 §1: outside-the-home the link is a VPN tunnel over 4G/5G, not the LAN. The
# latency floor is higher (typ. 40-120 ms one-way on 4G) so a fixed LAN threshold
# would flap network_degraded constantly. We keep two *network* profiles and lower
# the **video resolution target** on WAN (never the detector cadence on the PC —
# that runs locally; and never the device reflex paths — they don't need the PC).
LINK_LAN = "lan"
LINK_WAN = "wan"


@dataclass
class NetworkProfile:
    """Per-link network thresholds + the target video the client should send.

    ``target_video_height`` is a *hint the client honours* (720p on LAN, 540p on
    WAN by default) so the tunnel carries less video without touching any PC-side
    detector/OCR cadence. ``latency_soft_ms`` is where the StatusBar shows a WAN
    hint; ``latency_hard_ms`` (== DegradedThresholds.max_network_latency_ms) is
    where the network_degraded action fires."""

    link: str = LINK_LAN
    target_video_height: int = 720
    max_network_latency_ms: float = 250.0
    latency_soft_ms: float = 150.0
    max_frame_drops: int = 30


def default_network_profiles() -> dict[str, NetworkProfile]:
    return {
        LINK_LAN: NetworkProfile(link=LINK_LAN, target_video_height=720,
                                 max_network_latency_ms=250.0, latency_soft_ms=120.0,
                                 max_frame_drops=30),
        # WAN tolerates 4G/5G RTT (a Tailscale hop over mobile) and asks the client
        # for 540p so the tunnel is not saturated. PC detector cadence unchanged.
        LINK_WAN: NetworkProfile(link=LINK_WAN, target_video_height=540,
                                 max_network_latency_ms=400.0, latency_soft_ms=180.0,
                                 max_frame_drops=45),
    }


def network_profiles_from_config(cfg: dict[str, object] | None) -> dict[str, NetworkProfile]:
    """Merge a ``degraded.network`` config block over the defaults (all optional).

    Config shape (profile / rtx3070.yaml, all keys optional)::

        degraded:
          network:
            wan: {target_video_height: 480, max_network_latency_ms: 500}
            lan: {target_video_height: 720}
    """
    profiles = default_network_profiles()
    net = (cfg or {}).get("network") if isinstance(cfg, dict) else None
    if not isinstance(net, dict):
        return profiles
    for link in (LINK_LAN, LINK_WAN):
        block = net.get(link)
        if not isinstance(block, dict):
            continue
        p = profiles[link]
        if block.get("target_video_height") is not None:
            p.target_video_height = int(block["target_video_height"])
        if block.get("max_network_latency_ms") is not None:
            p.max_network_latency_ms = float(block["max_network_latency_ms"])
        if block.get("latency_soft_ms") is not None:
            p.latency_soft_ms = float(block["latency_soft_ms"])
        if block.get("max_frame_drops") is not None:
            p.max_frame_drops = int(block["max_frame_drops"])
    return profiles


def thresholds_for_link(
    profiles: dict[str, NetworkProfile], link: str, base: "DegradedThresholds | None" = None
) -> "DegradedThresholds":
    """Build a :class:`DegradedThresholds` whose network limits follow ``link``.

    GPU/heartbeat limits stay as the base (local, link-independent); only the
    network latency / drop ceilings track the active link's profile."""
    base = base or DegradedThresholds()
    p = profiles.get(link) or profiles[LINK_LAN]
    return DegradedThresholds(
        heartbeat_stale_s=base.heartbeat_stale_s,
        vram_floor_mb=base.vram_floor_mb,
        vram_warn_mb=base.vram_warn_mb,
        max_frame_drops=p.max_frame_drops,
        max_network_latency_ms=p.max_network_latency_ms,
    )


@dataclass
class DegradedSignals:
    now_ts: float                          # current monotonic/epoch seconds
    heartbeat_ts: float | None = None      # last PC/session heartbeat (same clock as now_ts)
    free_vram_mb: int | None = None        # from GpuArbiter snapshot; None = unknown
    frame_drops: int = 0                   # drops observed in the current window
    network_latency_ms: float | None = None


@dataclass
class DegradedState:
    active: bool = False
    reasons: list[str] = field(default_factory=list)
    action_level: str = ACTION_NOMINAL
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def event(self) -> dict[str, object]:
        return {
            "type": "degraded_state",
            "active": self.active,
            "reasons": list(self.reasons),
            "action_level": self.action_level,
            "updated_at": self.updated_at,
        }


class DegradedStateMachine:
    """Evaluate raw signals into a :class:`DegradedState`.

    The instance keeps the last computed state so callers can diff and only
    push an event to renderers when it changes.
    """

    def __init__(self, thresholds: DegradedThresholds | None = None) -> None:
        self.thresholds = thresholds or DegradedThresholds()
        self.state = DegradedState()

    def evaluate(self, signals: DegradedSignals) -> DegradedState:
        t = self.thresholds
        reasons: list[str] = []
        action = ACTION_NOMINAL

        # pc_absent: stale heartbeat -> reflexes must run on the device alone.
        if signals.heartbeat_ts is None or (signals.now_ts - signals.heartbeat_ts) > t.heartbeat_stale_s:
            reasons.append("pc_absent")
            action = _worse(action, ACTION_PC_UNAVAILABLE)

        # gpu_pressure: low free VRAM. Two-stage — warn floors the detector and
        # pauses change detection; critical additionally refuses the VLM.
        if signals.free_vram_mb is not None:
            if signals.free_vram_mb < t.vram_floor_mb:
                reasons.append("gpu_pressure_critical")
                action = _worse(action, ACTION_REFUSE_VLM)
            elif signals.free_vram_mb < t.vram_warn_mb:
                reasons.append("gpu_pressure")
                action = _worse(action, ACTION_PAUSE_CHANGES)

        # network_degraded: excessive drops or latency.
        network_bad = signals.frame_drops > t.max_frame_drops or (
            signals.network_latency_ms is not None
            and signals.network_latency_ms > t.max_network_latency_ms
        )
        if network_bad:
            reasons.append("network_degraded")
            action = _worse(action, ACTION_DETECTOR_FLOOR)

        self.state = DegradedState(
            active=bool(reasons),
            reasons=reasons,
            action_level=action,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        return self.state

    def evaluate_event(self, signals: DegradedSignals) -> dict[str, object]:
        return self.evaluate(signals).event()


def _worse(current: str, candidate: str) -> str:
    return candidate if _ACTION_RANK[candidate] > _ACTION_RANK[current] else current
