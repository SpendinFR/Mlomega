from __future__ import annotations
import secrets, time, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class ClockSample:
    client_send_ns: int
    server_recv_ns: int
    server_send_ns: int
    client_recv_ns: int
    offset_ns: int
    rtt_ns: int

@dataclass
class Session:
    session_id: str
    device_id: str
    token: str
    created_at_utc: str
    clock_samples: list[ClockSample] = field(default_factory=list)

class SessionHub:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._tokens: dict[str, str] = {}

    def create_session(self, device_id: str) -> Session:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        session_id = f"xr-{stamp}-{uuid.uuid4()}"
        token = secrets.token_urlsafe(32)
        session = Session(session_id=session_id, device_id=device_id, token=token, created_at_utc=datetime.now(timezone.utc).isoformat())
        self._sessions[session_id] = session
        self._tokens[token] = session_id
        return session

    def authenticate(self, token: str) -> Session | None:
        sid = self._tokens.get(token)
        return self._sessions.get(sid) if sid else None

    def begin_clock_sync(self) -> int:
        return time.monotonic_ns()

    def complete_clock_sync(self, session_id: str, client_send_ns: int, server_recv_ns: int, client_recv_ns: int, server_send_ns: int | None = None) -> ClockSample:
        if session_id not in self._sessions:
            raise KeyError(session_id)
        server_send_ns = server_recv_ns if server_send_ns is None else server_send_ns
        rtt_ns = (client_recv_ns - client_send_ns) - (server_send_ns - server_recv_ns)
        offset_ns = ((server_recv_ns - client_send_ns) + (server_send_ns - client_recv_ns)) // 2
        sample = ClockSample(client_send_ns, server_recv_ns, server_send_ns, client_recv_ns, offset_ns, rtt_ns)
        self._sessions[session_id].clock_samples.append(sample)
        return sample

    def current_offset_ns(self, session_id: str) -> int | None:
        samples = self._sessions[session_id].clock_samples
        if not samples:
            return None
        best = min(samples, key=lambda s: s.rtt_ns)
        return best.offset_ns
