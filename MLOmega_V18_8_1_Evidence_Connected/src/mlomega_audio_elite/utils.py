from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """UTC wall-clock timestamp with millisecond precision.

    V17.4 used second precision.  Several live writers can legitimately create
    distinct records in one second, so second precision is not a safe identifier
    component or audit timestamp.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")



def iso_add_seconds(base_iso: str | None, seconds: float | int | None) -> str | None:
    """Return `base_iso + seconds` as ISO-8601, preserving timezone when present.

    Conversation timestamps are absolute anchors; audio offsets are only relative.
    This helper is intentionally strict enough to keep bad timestamps visible while
    still falling back to the conversation anchor when no offset is available.
    """
    if not base_iso:
        return None
    if seconds is None:
        return base_iso
    try:
        offset = float(seconds)
    except (TypeError, ValueError):
        return base_iso
    raw = str(base_iso).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return base_iso
    out = dt + timedelta(seconds=offset)
    # Preserve sub-second offsets when present; 3-5s live chunks often need
    # fractional VAD boundaries for clean long-conversation assembly.
    return out.isoformat(timespec="milliseconds") if out.microsecond else out.isoformat()


def slugify(text: str, max_len: int = 80) -> str:
    text = normalize_text(text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len] or "item")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    """Deterministic identifier for *derived, replaceable* cache objects only.

    Do not use this for raw events or historical observations: identical content
    at the same second is still two occurrences.  V17.6 introduces ``new_id``
    for append-only facts.
    """
    payload = "|".join(json.dumps(p, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False) for p in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def new_id(prefix: str) -> str:
    """Random identifier for immutable observations and event facts."""
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dumps(value: Any) -> str:
    """Canonical JSON that rejects NaN/Infinity instead of serialising poison."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class JsonDecodeError(ValueError):
    """Raised by strict decoding when persisted data is corrupted."""


def json_loads_strict(value: str | None) -> Any:
    """Decode JSON without changing corruption into an empty value."""
    if value is None or value == "":
        raise JsonDecodeError("missing JSON payload")
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise JsonDecodeError(f"invalid JSON payload: {exc}") from exc


def json_loads(value: str | None, default: Any = None) -> Any:
    """Legacy compatibility loader.

    New V17.6 writers must use :func:`json_loads_strict` at trust boundaries.
    This wrapper remains only to avoid a flag-day migration of historical
    readers; it no longer serves as a validation mechanism.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def tokenize(text: str) -> list[str]:
    return re.findall(r"[\wÀ-ÖØ-öø-ÿ']+", normalize_text(text))
