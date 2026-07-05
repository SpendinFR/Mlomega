from __future__ import annotations

"""V19 live-PC HTTP front for :class:`SessionHub` + unified WebRTC signaling.

This is the E24 HTTP server that fronts the in-process :class:`SessionHub`
(``services/live-pc/sessionhub.py``) *without rewriting it*. It exposes exactly
the routes/JSON the Unity ``SessionHubClient.cs`` (E23) already speaks:

    POST /session/create      {device_id}
         -> {session_id, token, created_at_utc}          (SessionHub.create_session)
    POST /session/clock-sync  {session_id, token, client_send_ns}
         -> {server_recv_ns, server_send_ns}             (begin/complete_clock_sync)
    POST /session/renew       {session_id, token}
         -> {token, created_at_utc}                      (re-issue ephemeral token)
    GET  /health              -> readiness snapshot

Auth: ``/session/renew`` and ``/session/clock-sync`` require the ephemeral
session token issued by ``/session/create`` (``SessionHub.authenticate``). A
mismatched ``(session_id, token)`` pair is refused with HTTP 401.

Clock-sync arithmetic stays split exactly as the C# client expects: the server
returns the two server monotonic stamps (``server_recv_ns`` / ``server_send_ns``)
and the client computes the offset/RTT with the *same formulas* as
``SessionHub.complete_clock_sync`` (proven numerically in
``tests/v19/test_sessionhub_http.py``). The server also records the sample on
the session (via ``complete_clock_sync``) so ``current_offset_ns`` is available
server-side for degraded-mode/health, using the client-relayed
``client_send_ns`` and a server-observed ``client_recv_ns`` estimate.

Unified media signaling: ``POST /webrtc/offer`` (SDP offer in -> SDP answer out)
requires a valid session token and delegates to a single shared
:class:`AiortcIngress`, so ``simulators/fake_xr_device`` and the future Android
``LiveTransportPlugin`` negotiate through one stable endpoint instead of the
ingress' own ad-hoc ``/offer`` port.

Run standalone::

    python services/live-pc/sessionhub_http.py            # port 8710 (matches
                                                           # MLOmegaConfig.cs)

Port 8710 is the SessionHub HTTP port hard-wired in
``apps/xr-mobile/Assets/Scripts/Core/MLOmegaConfig.cs`` (87xx range, never 8766).
"""

import sys
import time
from pathlib import Path
from typing import Any

# Resolve the monorepo root so ``packages`` / sibling live-pc modules import
# whether launched as a script or loaded via importlib in tests.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ``sessionhub`` and ``gateway`` are sibling files in this non-package directory;
# load them by path so this module works under both plain execution and the
# importlib-based test harness used across tests/v19.
import importlib.util


def _load_sibling(name: str, filename: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_sessionhub = _load_sibling("sessionhub", "sessionhub.py")
SessionHub = _sessionhub.SessionHub
Session = _sessionhub.Session

# Gateway (aiortc) is optional; the SessionHub routes work without it. The
# /webrtc/offer route is only wired when aiortc is importable.
_gateway = _load_sibling("gateway", "gateway.py")

# Import FastAPI symbols at module scope. FastAPI resolves route annotations via
# typing.get_type_hints against the function's __globals__ (this module), not its
# closure — so ``Request`` MUST live here, not inside create_app, or the special
# Request parameter is mis-parsed as a query field.
try:
    from fastapi import FastAPI, HTTPException, Request

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - only without API deps
    FastAPI = None  # type: ignore[assignment,misc]
    HTTPException = Exception  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[assignment,misc]
    FASTAPI_AVAILABLE = False


DEFAULT_PORT = 8710  # MLOmegaConfig.cs SessionHubPort


def create_app(
    hub: "SessionHub | None" = None,
    ingress: Any | None = None,
    *,
    enable_signaling: bool = True,
):
    """Build the FastAPI app fronting ``hub`` and (optionally) media signaling.

    Parameters
    ----------
    hub:
        The :class:`SessionHub` to expose. A fresh one is created if omitted.
    ingress:
        An :class:`AiortcIngress` used for ``/webrtc/offer``. If omitted and
        aiortc is available, one is created lazily on first offer. Injected in
        tests to assert frame delivery.
    enable_signaling:
        When False, ``/webrtc/offer`` is not registered (SessionHub-only server).
    """
    if not FASTAPI_AVAILABLE:  # pragma: no cover - only without API deps
        raise RuntimeError("fastapi is required for sessionhub_http.create_app()")

    hub = hub or SessionHub()
    app = FastAPI(title="MLOmega V19 SessionHub HTTP")
    app.state.hub = hub
    app.state.ingress = ingress

    def _authenticate(session_id: str, token: str) -> "Session":
        session = hub.authenticate(token)
        if session is None or session.session_id != session_id:
            raise HTTPException(status_code=401, detail="invalid session token")
        return session

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "sessions": len(hub._sessions),
            "signaling": bool(enable_signaling) and _gateway.AIORTC_AVAILABLE,
        }

    @app.post("/session/create")
    async def create_session(request: Request) -> dict[str, Any]:
        body = await request.json()
        device_id = body.get("device_id")
        if not device_id or not isinstance(device_id, str):
            raise HTTPException(status_code=422, detail="device_id (str) is required")
        session = hub.create_session(device_id)
        return {
            "session_id": session.session_id,
            "token": session.token,
            "created_at_utc": session.created_at_utc,
        }

    @app.post("/session/renew")
    async def renew_token(request: Request) -> dict[str, Any]:
        body = await request.json()
        session_id = body.get("session_id")
        token = body.get("token")
        if not session_id or not token:
            raise HTTPException(status_code=422, detail="session_id and token are required")
        session = _authenticate(session_id, token)
        new_token = _reissue_token(hub, session)
        return {
            "token": new_token,
            # renew keeps the session id; refresh the timestamp so the client can
            # track token age. Matches SessionHubClient.RenewToken expectations.
            "created_at_utc": _now_iso(),
        }

    @app.post("/session/clock-sync")
    async def clock_sync(request: Request) -> dict[str, Any]:
        body = await request.json()
        session_id = body.get("session_id")
        token = body.get("token")
        client_send_ns = body.get("client_send_ns")
        if not session_id or not token or client_send_ns is None:
            raise HTTPException(
                status_code=422,
                detail="session_id, token and client_send_ns are required",
            )
        _authenticate(session_id, token)

        # One monotonic instant stamps both recv and send: the server is a single
        # point on its own clock for this exchange, exactly as SessionHub collapses
        # server_send_ns := server_recv_ns when unspecified. The C# client defaults
        # server_send := server_recv when the two are equal, so returning the same
        # value keeps the client math (ClockSync.ComputeSample) identical.
        server_stamp = hub.begin_clock_sync()

        # Record the sample server-side so current_offset_ns is available for
        # degraded-mode/health. client_recv_ns is unknown to the server, so we use
        # the same server_stamp as a lower-bound placeholder (the authoritative
        # offset is the client's; this is a coarse server-side mirror only).
        try:
            hub.complete_clock_sync(
                session_id,
                client_send_ns=int(client_send_ns),
                server_recv_ns=server_stamp,
                server_send_ns=server_stamp,
                client_recv_ns=int(client_send_ns),
            )
        except (KeyError, TypeError, ValueError):
            # A bad client_send_ns must not 500 the health of the exchange; the
            # stamps are still valid and the client owns the real computation.
            pass

        return {"server_recv_ns": server_stamp, "server_send_ns": server_stamp}

    if enable_signaling and _gateway.AIORTC_AVAILABLE:

        @app.post("/webrtc/offer")
        async def webrtc_offer(request: Request) -> dict[str, Any]:
            body = await request.json()
            session_id = body.get("session_id")
            token = body.get("token")
            sdp = body.get("sdp")
            sdp_type = body.get("type")
            if not sdp or not sdp_type:
                raise HTTPException(status_code=422, detail="sdp and type are required")
            if not session_id or not token:
                raise HTTPException(
                    status_code=422, detail="session_id and token are required"
                )
            _authenticate(session_id, token)

            active = app.state.ingress
            if active is None:
                active = _gateway.AiortcIngress(session_id=session_id)
                await active.start()
                app.state.ingress = active
            answer_sdp, answer_type = await active.handle_offer_sdp(sdp, sdp_type)
            return {"sdp": answer_sdp, "type": answer_type}

    return app


def _reissue_token(hub: "SessionHub", session: "Session") -> str:
    """Rotate the ephemeral token for ``session`` in place.

    Uses only public ``secrets``/existing hub state; the old token is revoked so
    a renewed client must present the new token. We do not rewrite SessionHub —
    we operate on its exposed mappings the same way ``create_session`` does.
    """
    import secrets

    old_token = session.token
    new_token = secrets.token_urlsafe(32)
    session.token = new_token
    hub._tokens.pop(old_token, None)
    hub._tokens[new_token] = session.session_id
    return new_token


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="MLOmega V19 SessionHub HTTP server")
    # E36 §1: bind on ALL interfaces by default so a VPN-tunnel (Tailscale 100.x)
    # peer can reach the SessionHub the same way a LAN peer does; the ephemeral
    # session token is the access barrier (already in place). Override with
    # ``--host`` or the profile's ``bind_host`` for a stricter bind.
    parser.add_argument(
        "--host", default=None,
        help="interface to bind (default: profile bind_host, else 0.0.0.0 — all interfaces)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-signaling",
        action="store_true",
        help="disable the /webrtc/offer media signaling route",
    )
    args = parser.parse_args(argv)

    import uvicorn

    host = args.host or _bind_host_from_profile() or "0.0.0.0"
    app = create_app(enable_signaling=not args.no_signaling)
    uvicorn.run(app, host=host, port=args.port)


def _bind_host_from_profile() -> str | None:
    """Read ``bind_host`` from configs/user_profile.yaml (E36 §1). Absent → None."""
    try:
        import yaml

        p = _ROOT / "configs" / "user_profile.yaml"
        if not p.exists():
            return None
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        host = data.get("bind_host")
        return str(host) if host else None
    except Exception:
        return None


if __name__ == "__main__":
    main()
