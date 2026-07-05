from __future__ import annotations

"""EndpointResolver — outside-the-home multi-endpoint failover (E36 §1).

The main use of MLOmega V19 is *outside* the home: the phone is on 4G/5G, the PC
sits at home behind NAT. A single hard-wired ``pc_host`` (E23 ``MLOmegaConfig``)
only works on the LAN. E36 makes the client accept an **ordered list of PC
endpoints** and pick the first one that is actually reachable:

    endpoints:
      - {name: lan,       host: 192.168.1.10, port: 8710}
      - {name: tailscale, host: 100.101.102.103, port: 8710}

Resolution rules (deterministic, local-first — no external relay/TURN by default):

* try the endpoints **in order** with a short ``GET /health`` probe;
* the first that answers ``ok`` becomes the ``active_endpoint``;
* if the active one later fails, :meth:`resolve` (or :meth:`on_disconnect`) falls
  through to the next reachable endpoint (**failover**);
* :meth:`resolve` always re-probes **from the top of the list** — so when the user
  comes back home the first (LAN) endpoint is chosen again automatically
  (return-home → return-LAN);
* when **no** endpoint answers, the resolver reports a clean *degraded* verdict
  (``pc_unreachable``) rather than raising — the device reflex paths (Ultra-Live)
  do not depend on the PC and keep running (handoff §3.6).

This module is intentionally free of the Python-only signaling stack: it takes a
``probe`` callable (default: a tiny urllib ``GET /health``) so it is trivially
testable against two localhost servers on different ports (LAN up → LAN chosen;
LAN port closed → failover; both down → pc_unreachable; first back → return).

The same ordered-list contract is mirrored on the clients: Unity
``MLOmegaConfig``/``SessionPairing`` (additive endpoint list) and Kotlin
``SignalingClient`` (list + failover). Python (``fake_xr_device`` + companion) use
this resolver directly.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

DEFAULT_PORT = 8710  # SessionHub HTTP port (MLOmegaConfig.cs)


# --------------------------------------------------------------------------- model
@dataclass
class Endpoint:
    """One PC endpoint the client may reach the SessionHub at."""

    name: str
    host: str
    port: int = DEFAULT_PORT
    use_tls: bool = False

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_tls else "http"
        return f"{scheme}://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    @property
    def webrtc_offer_url(self) -> str:
        return f"{self.base_url}/webrtc/offer"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "host": self.host, "port": self.port,
                "use_tls": self.use_tls, "base_url": self.base_url}


def parse_endpoints(raw: Any, *, default_port: int = DEFAULT_PORT) -> list[Endpoint]:
    """Build an ordered ``list[Endpoint]`` from config.

    Accepts (in priority order):

    * a list of ``{name, host, port?, use_tls?}`` mappings (the E36 shape);
    * a single ``{pc_host, session_hub_port?, use_tls?}`` (the E23 legacy shape) →
      one implicit ``lan`` endpoint (backward compatible: an old profile with only
      ``pc_host`` still resolves);
    * a bare host string.

    Unusable entries are skipped (never crashes a live session)."""
    out: list[Endpoint] = []

    def _one(entry: Any, fallback_name: str) -> Endpoint | None:
        if isinstance(entry, str):
            host = entry.strip()
            return Endpoint(name=fallback_name, host=host, port=default_port) if host else None
        if isinstance(entry, Mapping):
            host = str(entry.get("host") or entry.get("pc_host") or "").strip()
            if not host:
                return None
            port = int(entry.get("port") or entry.get("session_hub_port") or default_port)
            return Endpoint(
                name=str(entry.get("name") or fallback_name),
                host=host, port=port,
                use_tls=bool(entry.get("use_tls") or entry.get("useTls")),
            )
        return None

    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for i, entry in enumerate(raw):
            ep = _one(entry, f"endpoint{i + 1}")
            if ep is not None:
                out.append(ep)
    elif raw is not None:
        ep = _one(raw, "lan")
        if ep is not None:
            out.append(ep)
    return out


def endpoints_from_profile(profile: Mapping[str, Any], *, default_port: int = DEFAULT_PORT) -> list[Endpoint]:
    """Read the ordered endpoint list from a user profile.

    Prefers the E36 ``endpoints:`` list; falls back to the E23 single
    ``pc_host``/``session_hub_port`` so an un-migrated profile still works."""
    if profile.get("endpoints"):
        eps = parse_endpoints(profile.get("endpoints"), default_port=default_port)
        if eps:
            return eps
    host = profile.get("pc_host")
    if host:
        return parse_endpoints(
            {"pc_host": host, "session_hub_port": profile.get("session_hub_port"),
             "use_tls": profile.get("use_tls")},
            default_port=default_port,
        )
    return []


# --------------------------------------------------------------------------- probe
def default_health_probe(endpoint: Endpoint, *, timeout_s: float = 2.0) -> bool:
    """Short ``GET /health`` reachability probe (returns True on HTTP 200 ``ok``)."""
    try:
        with urllib.request.urlopen(endpoint.health_url, timeout=timeout_s) as resp:  # noqa: S310
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode("utf-8"))
            return str(body.get("status")) == "ok"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


# --------------------------------------------------------------------------- verdict
@dataclass
class ResolveResult:
    reachable: bool
    active: Endpoint | None
    tried: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "active_endpoint": self.active.name if self.active else None,
            "active_base_url": self.active.base_url if self.active else None,
            "tried": list(self.tried),
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- resolver
class EndpointResolver:
    """Ordered, failover-capable resolver over a list of PC endpoints.

    ``probe(endpoint) -> bool`` decides reachability (injected in tests). Always
    tries from the top of the list so the preferred (LAN) endpoint is reclaimed
    when it comes back — the device reflex layer is untouched either way."""

    def __init__(
        self,
        endpoints: Sequence[Endpoint],
        *,
        probe: Callable[[Endpoint], bool] | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        self.endpoints: list[Endpoint] = list(endpoints)
        self.timeout_s = timeout_s
        self._probe = probe or (lambda ep: default_health_probe(ep, timeout_s=timeout_s))
        self.active: Endpoint | None = None
        self.metrics = {"resolves": 0, "failovers": 0, "unreachable": 0}

    # ----------------------------------------------------------------- resolve
    def resolve(self) -> ResolveResult:
        """Probe endpoints top-to-bottom; the first reachable wins.

        A change of active endpoint from a previously-set one counts as a
        failover (metric). No reachable endpoint → ``pc_unreachable``."""
        self.metrics["resolves"] += 1
        tried: list[str] = []
        prev = self.active.name if self.active else None
        for ep in self.endpoints:
            tried.append(ep.name)
            if self._probe(ep):
                if prev is not None and prev != ep.name:
                    self.metrics["failovers"] += 1
                self.active = ep
                reason = "return_primary" if (prev is not None and ep is self.endpoints[0] and prev != ep.name) else "reachable"
                return ResolveResult(reachable=True, active=ep, tried=tried, reason=reason)
        # Nothing answered.
        self.active = None
        self.metrics["unreachable"] += 1
        return ResolveResult(reachable=False, active=None, tried=tried, reason="pc_unreachable")

    def on_disconnect(self) -> ResolveResult:
        """The active endpoint dropped mid-session → re-resolve (failover).

        Same as :meth:`resolve` but always starts from the top so a return to the
        LAN reclaims the primary endpoint."""
        return self.resolve()

    # ----------------------------------------------------------------- helpers
    def active_endpoint(self) -> Endpoint | None:
        return self.active

    def active_name(self) -> str | None:
        return self.active.name if self.active else None

    def resolve_with_retry(self, *, attempts: int = 1, delay_s: float = 0.0) -> ResolveResult:
        """Resolve with a few short retries (a tunnel can take a moment to route).

        Bounded and non-blocking by default (attempts=1). Never raises."""
        result = self.resolve()
        n = 1
        while not result.reachable and n < max(1, attempts):
            if delay_s > 0:
                time.sleep(delay_s)
            result = self.resolve()
            n += 1
        return result
