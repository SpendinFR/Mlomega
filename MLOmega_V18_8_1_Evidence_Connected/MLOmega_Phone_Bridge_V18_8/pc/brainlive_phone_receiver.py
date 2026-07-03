from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

try:
    from PIL import Image
    from PIL.ExifTags import GPSTAGS, TAGS
except Exception:  # Pillow is optional; install script installs it when possible.
    Image = None
    TAGS = {}
    GPSTAGS = {}

PROJECT_ROOT = Path(os.environ.get("MLOMEGA_PROJECT_ROOT", ".")).resolve()
TOKEN = os.environ.get("MLOMEGA_PHONE_TOKEN", "CHANGE_ME")
# ``MLOMEGA_PUMP_SECONDS`` is an explicit global override for troubleshooting.
# With no override, keep the tuned per-kind cadence below; the previous bridge
# exposed -PumpSeconds but never used it, which made configuration misleading.
_raw_global_pump = os.environ.get("MLOMEGA_PUMP_SECONDS", "").strip()
PUMP_SECONDS = float(_raw_global_pump) if _raw_global_pump else None

def _pump_seconds(kind_env: str, default: float) -> float:
    raw = os.environ.get(kind_env, "").strip()
    if raw:
        return max(0.01, float(raw))
    if PUMP_SECONDS is not None:
        return max(0.01, float(PUMP_SECONDS))
    return default

AUDIO_WORKERS = int(os.environ.get("MLOMEGA_AUDIO_WORKERS", "2"))
IMAGE_WORKERS = int(os.environ.get("MLOMEGA_IMAGE_WORKERS", "1"))
GPS_WORKERS = int(os.environ.get("MLOMEGA_GPS_WORKERS", "1"))
TRANSCRIPT_WORKERS = int(os.environ.get("MLOMEGA_TRANSCRIPT_WORKERS", "1"))
SESSION_WORKERS = int(os.environ.get("MLOMEGA_SESSION_WORKERS", "1"))
AUDIO_PUMP_SECONDS = _pump_seconds("MLOMEGA_AUDIO_PUMP_SECONDS", 0.15)
IMAGE_PUMP_SECONDS = _pump_seconds("MLOMEGA_IMAGE_PUMP_SECONDS", 0.5)
GPS_PUMP_SECONDS = _pump_seconds("MLOMEGA_GPS_PUMP_SECONDS", 0.5)
TRANSCRIPT_PUMP_SECONDS = _pump_seconds("MLOMEGA_TRANSCRIPT_PUMP_SECONDS", 0.5)
SESSION_PUMP_SECONDS = _pump_seconds("MLOMEGA_SESSION_PUMP_SECONDS", 1.0)
PERSON_ID = os.environ.get("MLOMEGA_PERSON_ID", "me")
# Kept under the historic environment name so the PowerShell switch
# -AllowPostStopOnSessionStop stays compatible.  In V18.4 it triggers the full
# gated close-day orchestrator, not a direct old-style purge.
ALLOW_POST_STOP = os.environ.get("MLOMEGA_ALLOW_POST_STOP", "0") == "1"
MAX_ATTEMPTS = int(os.environ.get("MLOMEGA_QUEUE_MAX_ATTEMPTS", "10"))
KEEP_QUEUE_BLOBS = os.environ.get("MLOMEGA_KEEP_QUEUE_BLOBS", "0") == "1"
CLEANUP_AFTER_POST_STOP = os.environ.get("MLOMEGA_PHONE_CLEANUP_AFTER_POST_STOP", "1") == "1"
CLEANUP_AFTER_LONGITUDINAL = os.environ.get("MLOMEGA_PHONE_CLEANUP_AFTER_LONGITUDINAL", "1") == "1"
DRAIN_BEFORE_POST_STOP = os.environ.get("MLOMEGA_PHONE_DRAIN_BEFORE_POST_STOP", "1") == "1"
DRAIN_TIMEOUT_S = float(os.environ.get("MLOMEGA_PHONE_DRAIN_TIMEOUT_S", "180"))
CLOSE_DAY_TIMEOUT_S = float(os.environ.get("MLOMEGA_CLOSE_DAY_TIMEOUT_S", "7200"))
CLOSE_DAY_POLL_S = float(os.environ.get("MLOMEGA_CLOSE_DAY_POLL_S", "2"))
CLEANUP_MEDIA_KINDS = {x.strip() for x in os.environ.get("MLOMEGA_PHONE_CLEANUP_MEDIA_KINDS", "audio,image").split(",") if x.strip()}
CLEANUP_DRY_RUN = os.environ.get("MLOMEGA_PHONE_CLEANUP_DRY_RUN", "0") == "1"

STATE_ROOT = Path(os.environ.get("MLOMEGA_BRIDGE_STATE_ROOT", str(PROJECT_ROOT / ".mlomega_audio_elite"))).expanduser().resolve()
BRAINLIVE_INBOX = STATE_ROOT / "brainlive_inbox"
QUEUE_ROOT = STATE_ROOT / "phone_bridge_queue"
BLOB_ROOT = QUEUE_ROOT / "files"
DB_PATH = QUEUE_ROOT / "queue.sqlite"
LOG_PATH = QUEUE_ROOT / "receiver.log"
SESSION_LOG_PATH = QUEUE_ROOT / "sessions.log"

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".aac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
TRANSCRIPT_EXTS = {".txt", ".json", ".jsonl"}

for p in [
    BRAINLIVE_INBOX / "audio",
    BRAINLIVE_INBOX / "images",
    BRAINLIVE_INBOX / "transcripts",
    BRAINLIVE_INBOX / "gps",
    BRAINLIVE_INBOX / "feedback",
    QUEUE_ROOT,
    BLOB_ROOT,
    QUEUE_ROOT / "session_events",
]:
    p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="MLOmega BrainLive Phone Receiver V18.8", version="18.8-adaptive-live-feedback-bridge")
_stop_pump = threading.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    line = f"{utc_now()} {msg}\n"
    print(line, end="")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def auth(x_mlomega_token: Optional[str]) -> None:
    if not TOKEN or TOKEN == "CHANGE_ME":
        raise HTTPException(status_code=500, detail="Server token is not configured")
    if x_mlomega_token != TOKEN:
        raise HTTPException(status_code=401, detail="Bad or missing X-MLomega-Token")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incoming_items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                original_name TEXT,
                blob_path TEXT,
                meta_json TEXT,
                source_event_id TEXT,
                sha256 TEXT,
                received_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                delivered_path TEXT,
                delivered_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incoming_status ON incoming_items(status, received_at)")
        # V17.5 queues can already exist.  Add the V18.4 source identity without
        # dropping any backlog rows, then use it to collapse transport retries.
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(incoming_items)").fetchall()}
        if "source_event_id" not in columns:
            conn.execute("ALTER TABLE incoming_items ADD COLUMN source_event_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incoming_source_event ON incoming_items(kind, source_event_id, received_at)")
        # If the receiver was killed mid-pump, retry those items on next startup.
        conn.execute("UPDATE incoming_items SET status='pending', last_error=COALESCE(last_error, 'reset from processing on startup') WHERE status='processing'")


init_db()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_ext(name: str, default: str = ".bin") -> str:
    ext = Path(name or "").suffix.lower()
    if not ext:
        guessed = mimetypes.guess_extension(name or "")
        ext = guessed or default
    ext = ext.replace("/", "").replace("\\", "")
    if len(ext) > 12:
        ext = default
    return ext


def parse_meta(meta: Optional[str]) -> dict[str, Any]:
    if not meta:
        return {}
    if isinstance(meta, str):
        meta = meta.strip()
    if not meta:
        return {}
    try:
        return json.loads(meta)
    except Exception:
        return {"raw_meta": meta}


def source_event_id_from_meta(meta: Optional[dict[str, Any]]) -> Optional[str]:
    """Return the stable capture identity carried across Android HTTP retries."""
    m = dict(meta or {})
    value = _first_nonempty(m, "source_event_id", "event_id", "capture_event_id")
    if value is None and isinstance(m.get("session_event"), dict):
        nested = m["session_event"]
        value = _first_nonempty(nested, "source_event_id", "event_id", "capture_event_id")
    text = str(value or "").strip()
    return text[:300] if text else None


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + f".tmp-{uuid.uuid4().hex}")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def classify_filename(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TRANSCRIPT_EXTS:
        return "transcript"
    raise HTTPException(status_code=400, detail=f"Unsupported extension: {ext}")


def dms_to_decimal(value: Any) -> Optional[float]:
    try:
        d, m, s = value
        def rat(x: Any) -> float:
            if isinstance(x, tuple):
                return float(x[0]) / float(x[1])
            return float(x)
        return rat(d) + rat(m) / 60.0 + rat(s) / 3600.0
    except Exception:
        return None


def image_exif_gps(path: Path) -> Optional[dict[str, Any]]:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            gps_raw = None
            for tag_id, value in exif.items():
                if TAGS.get(tag_id, tag_id) == "GPSInfo":
                    gps_raw = value
                    break
            if not gps_raw:
                return None
            gps = {}
            for k, v in gps_raw.items():
                gps[GPSTAGS.get(k, k)] = v
            lat = dms_to_decimal(gps.get("GPSLatitude"))
            lon = dms_to_decimal(gps.get("GPSLongitude"))
            if lat is None or lon is None:
                return None
            if gps.get("GPSLatitudeRef") in ["S", b"S"]:
                lat = -lat
            if gps.get("GPSLongitudeRef") in ["W", b"W"]:
                lon = -lon
            return {
                "lat": lat,
                "lon": lon,
                "source": "image_exif",
                "captured_at": utc_now(),
                "confidence": 0.7,
            }
    except Exception:
        return None



def _first_nonempty(meta: dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        value = meta.get(key)
        if value not in (None, ""):
            return value
    return None


def load_current_gps() -> Optional[dict[str, Any]]:
    gps_path = BRAINLIVE_INBOX / "gps" / "current.json"
    try:
        if gps_path.exists():
            data = json.loads(gps_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "lat" in data and "lon" in data:
                return data
    except Exception:
        return None
    return None


def normalized_sidecar(kind: str, target: Path, row: sqlite3.Row, meta: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Normalize Android sidecar metadata to the names BrainLive/V15.14 expect."""
    m = dict(meta or {})
    received_at = row["received_at"]
    timestamp_start = _first_nonempty(m, "timestamp_start", "started_at", "captured_at", "recorded_at", "start_at", "created_at") or received_at
    timestamp_end = _first_nonempty(m, "timestamp_end", "ended_at", "captured_at", "end_at") or timestamp_start
    out: dict[str, Any] = dict(m)
    out.update({
        "type": kind,
        "media_kind": kind,
        "received_from": m.get("received_from", "android"),
        "source_device": m.get("source_device", "android_phone"),
        "timestamp_start": timestamp_start,
        "timestamp_end": timestamp_end,
        "captured_at": _first_nonempty(m, "captured_at", "timestamp_start", "started_at") or timestamp_start,
        "received_at": received_at,
        "delivered_at": utc_now(),
        "delivered_file": str(target),
        "original_name": row["original_name"],
        "queue_id": row["id"],
        "source_event_id": row["source_event_id"] or source_event_id_from_meta(m) or f"receiver:{row['id']}",
        "sha256": row["sha256"],
        "v18_phone_bridge_version": "18.7_resumable_close_day",
        "v17_phone_bridge_version": "17.5_quality_capture_compatible",
    })
    if kind == "audio":
        out.setdefault("capture_profile", "quality_voice_v17_5")
        out.setdefault("audio_priority", "quality_first")
        out.setdefault("expected_downstream", ["whisper_asr", "speechbrain_voice_match", "brainlive_turns", "brain2_offline_assembly"])
        gps = load_current_gps()
        if gps and "gps" not in out:
            out["gps"] = gps
            out["gps_source"] = "receiver_current_gps_at_delivery"
    elif kind == "image":
        out.setdefault("capture_profile", "vlm_scene_v17_5")
        out.setdefault("image_priority", "scene_understanding")
        out.setdefault("expected_downstream", ["moondream_live_vlm", "brain2_offline_deep_vlm", "scene_context", "place_context"])
        gps = out.get("gps") or load_current_gps()
        if gps and "gps" not in out:
            out["gps"] = gps
            out["gps_source"] = "receiver_current_gps_at_delivery"
    return out


def write_media_sidecar(kind: str, target: Path, row: sqlite3.Row, meta: Optional[dict[str, Any]]) -> None:
    sidecar = target.with_suffix(target.suffix + ".json")
    atomic_write_json(sidecar, normalized_sidecar(kind, target, row, meta))

def enqueue(kind: str, original_name: str, blob_path: Optional[Path], meta: dict[str, Any]) -> tuple[str, bool]:
    """Queue one source item and collapse retried Android uploads.

    ``source_event_id`` is created on Android before the first HTTP request.  A
    second request with that identity must return the original queue item, not
    create another raw file.  A changed payload for the same identity is an
    explicit collision rather than a silent rewrite.
    """
    item_id = uuid.uuid4().hex
    sha = sha256_file(blob_path) if blob_path and blob_path.exists() else None
    source_event_id = source_event_id_from_meta(meta)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = None
            if source_event_id:
                existing = conn.execute(
                    """SELECT * FROM incoming_items
                       WHERE kind=? AND source_event_id=?
                       ORDER BY received_at ASC LIMIT 1""",
                    (kind, source_event_id),
                ).fetchone()
            if existing:
                existing_sha = str(existing["sha256"] or "")
                if sha and existing_sha and sha != existing_sha:
                    raise ValueError(f"source_event_id collision for {kind}:{source_event_id}")
                # A transport retry can revive a previously exhausted local
                # delivery item, while retaining the same durable identity.
                if str(existing["status"]) == "failed":
                    conn.execute(
                        """UPDATE incoming_items
                           SET blob_path=?, meta_json=?, sha256=COALESCE(?,sha256),
                               status='pending', attempts=0, last_error=NULL
                           WHERE id=?""",
                        (str(blob_path) if blob_path else existing["blob_path"], json.dumps(meta, ensure_ascii=False), sha, existing["id"]),
                    )
                conn.execute("COMMIT")
                return str(existing["id"]), True
            conn.execute(
                """
                INSERT INTO incoming_items
                (id, kind, original_name, blob_path, meta_json, source_event_id, sha256, received_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    item_id,
                    kind,
                    original_name,
                    str(blob_path) if blob_path else None,
                    json.dumps(meta, ensure_ascii=False),
                    source_event_id,
                    sha,
                    utc_now(),
                ),
            )
            conn.execute("COMMIT")
            return item_id, False
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise


async def save_upload_to_queue(file: UploadFile, kind: str, meta: dict[str, Any]) -> tuple[str, bool]:
    ext = safe_ext(file.filename or "upload.bin")
    blob_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex}{ext}"
    blob_path = BLOB_ROOT / kind / blob_name
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = blob_path.with_suffix(blob_path.suffix + ".uploading")
    with tmp_path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp_path, blob_path)

    if kind == "image":
        gps = image_exif_gps(blob_path)
        if gps and "gps" not in meta:
            meta["gps"] = gps
            meta["gps_from_image_exif"] = True

    try:
        item_id, reused = enqueue(kind, file.filename or blob_name, blob_path, meta)
    except Exception:
        blob_path.unlink(missing_ok=True)
        raise
    if reused and not KEEP_QUEUE_BLOBS:
        # The prior queue row is the canonical owner of this capture.
        blob_path.unlink(missing_ok=True)
    return item_id, reused


def target_for_row(row: sqlite3.Row) -> tuple[Optional[Path], Optional[dict[str, Any]]]:
    kind = row["kind"]
    meta = json.loads(row["meta_json"] or "{}")
    received = row["received_at"].replace(":", "-").replace("+", "Z")
    ext = Path(row["original_name"] or "").suffix.lower()
    source_event_id = row["source_event_id"] or source_event_id_from_meta(meta)
    # Same Android event -> same inbox filename even when an HTTP retry enters
    # the receiver after a local crash.  Legacy records retain queue-id naming.
    identity = source_event_id or f"queue:{row['id']}"
    item_key = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:16]

    if kind == "audio":
        return BRAINLIVE_INBOX / "audio" / f"phone_{received}_{item_key}{ext or '.wav'}", meta
    if kind == "image":
        return BRAINLIVE_INBOX / "images" / f"phone_{received}_{item_key}{ext or '.jpg'}", meta
    if kind == "transcript":
        return BRAINLIVE_INBOX / "transcripts" / f"phone_{received}_{item_key}{ext or '.json'}", None
    if kind == "gps":
        return BRAINLIVE_INBOX / "gps" / "current.json", meta
    if kind == "session_event":
        return QUEUE_ROOT / "session_events" / f"session_{received}_{item_key}.json", meta
    return None, None


def claim_one(kind: str) -> Optional[sqlite3.Row]:
    """Atomically claim one pending item of a given kind.

    Separate worker pools call this for audio/image/gps in parallel.  The
    BEGIN IMMEDIATE transaction avoids two workers claiming the same row while
    keeping SQLite simple and robust on Windows.
    """
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM incoming_items
            WHERE status='pending' AND kind=?
            ORDER BY received_at ASC
            LIMIT 1
            """,
            (kind,),
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE incoming_items SET status='processing', attempts=attempts+1 WHERE id=? AND status='pending'",
            (row["id"],),
        )
        conn.execute("COMMIT")
        return row
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def deliver_row(row: sqlite3.Row) -> bool:
    try:
        target, meta = target_for_row(row)
        if target is None:
            raise RuntimeError(f"No target for kind={row['kind']}")

        kind = row["kind"]
        if kind == "gps":
            gps = dict(meta or {})
            # Do not let non-location payloads overwrite gps/current.json.
            if "lat" not in gps or "lon" not in gps:
                raise RuntimeError("GPS payload missing lat/lon; refusing to overwrite current.json")
            gps.setdefault("source", "android")
            gps.setdefault("updated_at", utc_now())
            gps.setdefault("confidence", 0.8)
            atomic_write_json(target, gps)
            delivered = str(target)
        elif kind == "session_event":
            event = dict(meta or {})
            event.setdefault("received_at", utc_now())
            atomic_write_json(target, event)
            with SESSION_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            delivered = str(target)
        else:
            blob = Path(row["blob_path"])
            if not blob.exists():
                raise FileNotFoundError(str(blob))
            atomic_copy(blob, target)
            delivered = str(target)

            # Keep normalized sidecar metadata next to media.  BrainLive/V15.14
            # can then anchor chunks by phone capture time and correlate audio/image/GPS.
            if kind in {"audio", "image"}:
                write_media_sidecar(kind, target, row, meta)

            if not KEEP_QUEUE_BLOBS:
                try:
                    blob.unlink(missing_ok=True)
                except Exception:
                    pass

        with db() as conn:
            conn.execute(
                "UPDATE incoming_items SET status='delivered', delivered_path=?, delivered_at=?, last_error=NULL WHERE id=?",
                (delivered, utc_now(), row["id"]),
            )
        log(f"delivered kind={kind} id={row['id']} -> {delivered}")
        return True
    except Exception as e:
        next_status = "failed" if int(row["attempts"] or 0) + 1 >= MAX_ATTEMPTS else "pending"
        with db() as conn:
            conn.execute(
                "UPDATE incoming_items SET status=?, last_error=? WHERE id=?",
                (next_status, repr(e), row["id"]),
            )
        log(f"pump error id={row['id']} kind={row['kind']} status={next_status} error={repr(e)}")
        return False


def pump_one_kind(kind: str) -> bool:
    row = claim_one(kind)
    if not row:
        return False
    return deliver_row(row)


def worker_loop(kind: str, sleep_s: float, worker_index: int) -> None:
    log(f"worker started kind={kind} index={worker_index} sleep={sleep_s}s")
    while not _stop_pump.is_set():
        try:
            worked = pump_one_kind(kind)
            if not worked:
                time.sleep(sleep_s)
        except Exception as e:
            log(f"worker error kind={kind} index={worker_index} error={repr(e)}")
            time.sleep(sleep_s)


def start_worker_pool() -> None:
    # Audio has its own small pool and shortest sleep: it is the priority path.
    # Images and GPS have separate workers, so a large image copy never blocks
    # audio delivery.  This keeps audio/image/gps close in time without making
    # all media wait behind one global FIFO.
    specs: list[tuple[str, int, float]] = [
        ("audio", AUDIO_WORKERS, AUDIO_PUMP_SECONDS),
        ("image", IMAGE_WORKERS, IMAGE_PUMP_SECONDS),
        ("gps", GPS_WORKERS, GPS_PUMP_SECONDS),
        ("transcript", TRANSCRIPT_WORKERS, TRANSCRIPT_PUMP_SECONDS),
        ("session_event", SESSION_WORKERS, SESSION_PUMP_SECONDS),
    ]
    for kind, n, sleep_s in specs:
        for i in range(max(0, n)):
            t = threading.Thread(target=worker_loop, args=(kind, sleep_s, i + 1), name=f"phone-pump-{kind}-{i+1}", daemon=True)
            t.start()


def pump_one() -> bool:
    """Compatibility helper for /pump-now: pump one item per kind in priority order."""
    for kind in ["audio", "image", "gps", "transcript", "session_event"]:
        if pump_one_kind(kind):
            return True
    return False


@app.on_event("startup")
def startup() -> None:
    init_db()
    log(f"parallel pool started root={PROJECT_ROOT} inbox={BRAINLIVE_INBOX} audio_workers={AUDIO_WORKERS} image_workers={IMAGE_WORKERS} gps_workers={GPS_WORKERS}")
    start_worker_pool()


@app.on_event("shutdown")
def shutdown() -> None:
    _stop_pump.set()


@app.get("/health")
def health() -> dict[str, Any]:
    with db() as conn:
        counts = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) AS n FROM incoming_items GROUP BY status").fetchall()
        }
    return {
        "ok": True,
        "project_root": str(PROJECT_ROOT),
        "brainlive_inbox": str(BRAINLIVE_INBOX),
        "queue_db": str(DB_PATH),
        "state_root": str(STATE_ROOT),
        "counts": counts,
        "allow_post_stop": ALLOW_POST_STOP,
        "allow_close_day_on_session_stop": ALLOW_POST_STOP,
        "keep_queue_blobs": KEEP_QUEUE_BLOBS,
        "cleanup_after_post_stop": CLEANUP_AFTER_POST_STOP,
        "cleanup_after_longitudinal": CLEANUP_AFTER_LONGITUDINAL,
        "close_day_timeout_s": CLOSE_DAY_TIMEOUT_S,
        "close_day_poll_s": CLOSE_DAY_POLL_S,
        "receiver_version": "18.8_adaptive_live_feedback",
        "delivery_feedback_endpoint": "/interventions/feedback",
        "cleanup_media_kinds": sorted(CLEANUP_MEDIA_KINDS),
        "cleanup_dry_run": CLEANUP_DRY_RUN,
        "drain_before_post_stop": DRAIN_BEFORE_POST_STOP,
        "drain_timeout_s": DRAIN_TIMEOUT_S,
        "max_attempts": MAX_ATTEMPTS,
        "workers": {
            "audio": AUDIO_WORKERS,
            "image": IMAGE_WORKERS,
            "gps": GPS_WORKERS,
            "transcript": TRANSCRIPT_WORKERS,
            "session_event": SESSION_WORKERS,
        },
        "worker_sleep_seconds": {
            "audio": AUDIO_PUMP_SECONDS,
            "image": IMAGE_PUMP_SECONDS,
            "gps": GPS_PUMP_SECONDS,
            "transcript": TRANSCRIPT_PUMP_SECONDS,
            "session_event": SESSION_PUMP_SECONDS,
        },
    }


@app.get("/status")
def status(x_mlomega_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    auth(x_mlomega_token)
    with db() as conn:
        counts = {
            f"{r['kind']}:{r['status']}": r["n"]
            for r in conn.execute(
                "SELECT kind, status, COUNT(*) AS n FROM incoming_items GROUP BY kind, status"
            ).fetchall()
        }
        last = [dict(r) for r in conn.execute(
            "SELECT id, kind, original_name, received_at, status, delivered_path, last_error FROM incoming_items ORDER BY received_at DESC LIMIT 20"
        ).fetchall()]
    return {"ok": True, "counts": counts, "last": last}


@app.post("/upload/audio")
async def upload_audio(
    file: UploadFile = File(...),
    meta: Optional[str] = Form(default=None),
    x_mlomega_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    auth(x_mlomega_token)
    m = parse_meta(meta)
    m.setdefault("received_from", "android")
    item_id, reused = await save_upload_to_queue(file, "audio", m)
    return {"ok": True, "queued_id": item_id, "kind": "audio", "reused": reused}


@app.post("/upload/image")
async def upload_image(
    file: UploadFile = File(...),
    meta: Optional[str] = Form(default=None),
    x_mlomega_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    auth(x_mlomega_token)
    m = parse_meta(meta)
    m.setdefault("received_from", "android")
    item_id, reused = await save_upload_to_queue(file, "image", m)
    return {"ok": True, "queued_id": item_id, "kind": "image", "reused": reused}


@app.post("/upload/transcript")
async def upload_transcript(
    file: UploadFile = File(...),
    meta: Optional[str] = Form(default=None),
    x_mlomega_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    auth(x_mlomega_token)
    m = parse_meta(meta)
    m.setdefault("received_from", "android")
    item_id, reused = await save_upload_to_queue(file, "transcript", m)
    return {"ok": True, "queued_id": item_id, "kind": "transcript", "reused": reused}


@app.post("/upload/auto")
async def upload_auto(
    file: UploadFile = File(...),
    meta: Optional[str] = Form(default=None),
    x_mlomega_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    auth(x_mlomega_token)
    kind = classify_filename(file.filename or "")
    m = parse_meta(meta)
    m.setdefault("received_from", "android")
    item_id, reused = await save_upload_to_queue(file, kind, m)
    return {"ok": True, "queued_id": item_id, "kind": kind, "reused": reused}


@app.post("/gps")
async def upload_gps(request: Request, x_mlomega_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    auth(x_mlomega_token)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON GPS payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="GPS payload must be an object")
    payload.setdefault("received_from", "android")
    payload.setdefault("received_at", utc_now())
    item_id, reused = enqueue("gps", "current.json", None, payload)
    return {"ok": True, "queued_id": item_id, "kind": "gps", "reused": reused}


@app.post("/interventions/feedback")
async def intervention_feedback(request: Request, x_mlomega_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Persist a receipt for one live intervention for BrainLive/Brain2.

    The receiver deliberately writes a durable JSON event into the core inbox
    rather than claiming that the intervention caused any outcome.  BrainLive
    records the actual lifecycle (delivered/displayed/seen/acted/dismissed/
    ignored); Brain2 later reconciles it with subsequent evidence.
    """
    auth(x_mlomega_token)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON feedback payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Feedback payload must be an object")
    delivery_id = str(payload.get("delivery_id") or "").strip()
    feedback_type = str(payload.get("feedback_type") or payload.get("status") or "").strip().lower()
    allowed = {"delivered", "displayed", "seen", "acted", "dismissed", "ignored", "failed"}
    if not delivery_id:
        raise HTTPException(status_code=400, detail="delivery_id is required")
    if feedback_type not in allowed:
        raise HTTPException(status_code=400, detail=f"feedback_type must be one of {sorted(allowed)}")
    payload["delivery_id"] = delivery_id
    payload["feedback_type"] = feedback_type
    payload.setdefault("feedback_source", "phone_bridge")
    payload.setdefault("source_device", "phone_bridge")
    payload.setdefault("observed_at", utc_now())
    payload.setdefault("received_at", utc_now())
    explicit_id = str(payload.get("feedback_id") or "").strip()
    identity = explicit_id or hashlib.sha256(
        json.dumps({
            "delivery_id": delivery_id,
            "feedback_type": feedback_type,
            "observed_at": payload["observed_at"],
            "note": payload.get("note"),
            "evidence": payload.get("evidence") or {},
            "feedback_source": payload.get("feedback_source"),
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    payload["feedback_id"] = identity
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", identity)[:160] or hashlib.sha256(identity.encode("utf-8")).hexdigest()
    target = BRAINLIVE_INBOX / "feedback" / f"{safe_id}.json"
    if target.exists():
        return {"ok": True, "feedback_id": identity, "delivery_id": delivery_id, "reused": True, "inbox_path": str(target)}
    atomic_write_json(target, payload)
    target.with_suffix(target.suffix + ".ready").write_text("ready\n", encoding="utf-8")
    log(f"intervention feedback queued delivery_id={delivery_id} type={feedback_type} feedback_id={identity}")
    return {"ok": True, "feedback_id": identity, "delivery_id": delivery_id, "reused": False, "inbox_path": str(target)}


@app.post("/session/start")
async def session_start(request: Request, x_mlomega_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    auth(x_mlomega_token)
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        pass
    payload.setdefault("event", "phone_capture_start")
    payload.setdefault("received_at", utc_now())
    item_id, reused = enqueue("session_event", "session_start.json", None, {"session_event": payload})
    log(f"session start {payload}")
    return {"ok": True, "queued_id": item_id, "reused": reused}


def pending_count_db() -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM incoming_items WHERE status IN ('pending','processing')").fetchone()
        return int(row["n"] if row else 0)


def drain_queue_until_idle(timeout_s: float = DRAIN_TIMEOUT_S) -> int:
    """Drain pending queue before post-stop so Brain2 sees the last phone chunks."""
    deadline = time.time() + max(1.0, timeout_s)
    pumped = 0
    while time.time() < deadline:
        worked = False
        for kind in ["audio", "gps", "image", "transcript", "session_event"]:
            if pump_one_kind(kind):
                pumped += 1
                worked = True
        if pending_count_db() == 0:
            break
        if not worked:
            time.sleep(0.25)
    log(f"drain before post-stop pumped={pumped} remaining={pending_count_db()}")
    return pumped


def _json_from_command_output(text: str) -> Optional[dict[str, Any]]:
    """CLI commands print JSON, but progress lines may precede it."""
    raw = (text or "").strip()
    if not raw:
        return None
    candidates = [raw, *reversed(raw.splitlines())]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except Exception:
            continue
    return None


def run_mlomega_command(args: list[str]) -> dict[str, Any]:
    """Run the V18 CLI and preserve a structured answer for orchestration."""
    exe = PROJECT_ROOT / ".venv" / "Scripts" / "mlomega-audio.exe"
    if not exe.exists():
        msg = f"command skipped; missing {exe}"
        log(msg)
        return {"ok": False, "args": args, "returncode": None, "result": None, "error": msg}
    cmd = [str(exe), *args]
    log("command starting: " + " ".join(cmd))
    try:
        cp = subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
        stdout = cp.stdout or ""
        stderr = cp.stderr or ""
        result = _json_from_command_output(stdout)
        log(f"command finished rc={cp.returncode}: " + " ".join(cmd))
        if stderr.strip():
            log("command stderr: " + stderr.strip()[:1200])
        if cp.returncode != 0 and stdout.strip():
            log("command stdout: " + stdout.strip()[:1200])
        return {
            "ok": cp.returncode == 0,
            "args": args,
            "returncode": cp.returncode,
            "result": result,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
    except Exception as e:
        log(f"command failed {repr(e)}: " + " ".join(cmd))
        return {"ok": False, "args": args, "returncode": None, "result": None, "error": repr(e)}


def _close_day_result_is_safe(result: Optional[dict[str, Any]]) -> bool:
    if not isinstance(result, dict):
        return False
    cleanup = result.get("cleanup") or {}
    return str(result.get("status") or "").lower() == "completed" and bool(cleanup.get("eligible"))


def wait_for_close_day(service_run_id: Optional[str], *, timeout_s: float = CLOSE_DAY_TIMEOUT_S) -> dict[str, Any]:
    """Wait for the service-owned close-day flow after requesting a stop."""
    deadline = time.time() + max(1.0, timeout_s)
    latest: dict[str, Any] = {}
    while time.time() < deadline:
        args = ["brainlive-status"]
        if service_run_id:
            args.extend(["--service-run-id", service_run_id])
        report = run_mlomega_command(args)
        if not report.get("ok"):
            latest = report
            time.sleep(max(0.2, CLOSE_DAY_POLL_S))
            continue
        state = report.get("result") or {}
        latest = state if isinstance(state, dict) else report
        close_day = state.get("close_day") if isinstance(state, dict) else None
        if _close_day_result_is_safe(close_day):
            return {"ok": True, "result": close_day, "service_status": state}
        if isinstance(close_day, dict) and str(close_day.get("status") or "").lower() == "retryable_error":
            # A complete durable checkpoint exists; tell the caller to resume it
            # now instead of wasting the bridge thread until its long poll timeout.
            return {"ok": False, "result": close_day, "service_status": state, "error": "close_day_retryable"}
        if isinstance(close_day, dict) and str(close_day.get("status") or "").lower() in {"failed", "error", "quarantined", "blocked"}:
            return {"ok": False, "result": close_day, "service_status": state, "error": "close_day_failed"}
        time.sleep(max(0.2, CLOSE_DAY_POLL_S))
    return {"ok": False, "result": latest, "error": "close_day_timeout"}


def cleanup_phone_media(cutoff_iso: Optional[str] = None, reason: str = "manual") -> dict[str, Any]:
    """Delete raw phone audio/images from BrainLive inbox after successful Brain2 consolidation."""
    cutoff = None
    if cutoff_iso:
        try:
            cutoff = datetime.fromisoformat(cutoff_iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            cutoff = None
    deleted: list[str] = []
    kept: list[str] = []
    errors: list[str] = []
    dirs = []
    if "audio" in CLEANUP_MEDIA_KINDS:
        dirs.append(BRAINLIVE_INBOX / "audio")
    if "image" in CLEANUP_MEDIA_KINDS or "images" in CLEANUP_MEDIA_KINDS:
        dirs.append(BRAINLIVE_INBOX / "images")
    manifest_path = QUEUE_ROOT / "cleanup_manifests" / f"cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    for d in dirs:
        if not d.exists():
            continue
        for path in sorted(d.glob("phone_*")):
            if not path.is_file():
                continue
            try:
                if cutoff is not None and path.stat().st_mtime > cutoff + 60:
                    kept.append(str(path))
                    continue
                rec = {"path": str(path), "bytes": path.stat().st_size, "mtime": path.stat().st_mtime}
                if not CLEANUP_DRY_RUN:
                    path.unlink(missing_ok=True)
                deleted.append(json.dumps(rec, ensure_ascii=False))
            except Exception as e:
                errors.append(f"{path}: {repr(e)}")
    manifest = {
        "reason": reason,
        "cutoff_iso": cutoff_iso,
        "dry_run": CLEANUP_DRY_RUN,
        "deleted_count": len(deleted),
        "kept_count": len(kept),
        "errors": errors,
        "deleted": [json.loads(x) for x in deleted[:5000]],
    }
    atomic_write_json(manifest_path, manifest)
    log(f"cleanup phone media reason={reason} deleted={len(deleted)} kept={len(kept)} errors={len(errors)} manifest={manifest_path}")
    return {"ok": not errors, "manifest": str(manifest_path), **{k: v for k, v in manifest.items() if k != "deleted"}}


def _active_service_for_explicit_stop() -> dict[str, Any]:
    """Return the current service identity from the same project runtime manifest.

    A phone stop must never fall back to the globally newest service run: that
    can target another capture on a shared PC.  The detached RUN wrapper owns
    this manifest and writes the person, session and exact service_run_id.
    """
    report = run_mlomega_command(["brainlive-runtime-status"])
    if not report.get("ok"):
        return {"state": "unknown", "error": report.get("error") or report.get("stderr") or "runtime_status_failed"}
    payload = report.get("result") or {}
    service = payload.get("service") if isinstance(payload, dict) else None
    if not isinstance(service, dict):
        return {"state": "unknown", "error": "runtime_manifest_missing"}
    status = str(service.get("status") or "").lower()
    run_id = str(service.get("service_run_id") or "").strip()
    owner = str(service.get("person_id") or "").strip()
    if status == "running":
        if not run_id:
            return {"state": "unknown", "error": "running_service_without_service_run_id"}
        if owner and owner != PERSON_ID:
            return {"state": "unknown", "error": f"runtime_owner_mismatch:{owner}"}
        return {"state": "running", "service_run_id": run_id}
    if status in {"stopped", "completed", "stop_requested", "stopped_pending_ingest", "orphaned", "drain_recovery", ""}:
        return {"state": "not_running", "status": status}
    return {"state": "unknown", "error": f"unexpected_runtime_status:{status}"}


def run_close_day() -> None:
    """Historic one-stop Phone behavior, now gated by V18.8 resumable close-day.

    The public PowerShell switch remains ``-AllowPostStopOnSessionStop``.
    It drains phone uploads, asks the running service to stop with ``--close-day``.
    BrainLive V18.8 then performs its own final acknowledged inbox drain before
    post-stop, so receiver delivery is not mistaken for ingestion completion.
    and permits a raw-media purge only after V13-V17/Life Model/live-ready and
    V18 manifests all report success.
    """
    if DRAIN_BEFORE_POST_STOP:
        drain_queue_until_idle(DRAIN_TIMEOUT_S)
    cutoff_iso = utc_now()
    active = _active_service_for_explicit_stop()
    outcome: dict[str, Any]
    if active.get("state") == "running":
        service_run_id = str(active["service_run_id"])
        request = run_mlomega_command(["brainlive-stop-service", "--service-run-id", service_run_id, "--close-day"])
        if request.get("ok") and isinstance(request.get("result"), dict) and str(request["result"].get("status")) == "stop_requested":
            outcome = wait_for_close_day(service_run_id)
        else:
            outcome = {"ok": False, "result": request.get("result"), "error": request.get("error") or "stop_request_failed"}
    elif active.get("state") == "not_running":
        # A manual stop or power loss can leave a retained inbox. Acknowledge
        # it first, then resume the exact close-day checkpoint; neither command
        # creates a duplicate run.
        drain = run_mlomega_command(["brainlive-resume-inbox-drain", "--person-id", PERSON_ID])
        direct = run_mlomega_command(["brainlive-resume-close-day", "--person-id", PERSON_ID, "--force"])
        outcome = {"ok": bool(drain.get("ok")) and bool(direct.get("ok")) and _close_day_result_is_safe(direct.get("result")), "result": direct.get("result"), "direct": True, "drain": drain}
    else:
        # Fail closed rather than selecting another running session. The Android
        # upload spool and PC raw evidence remain intact; an operator can run
        # RESUME once the runtime manifest is repaired.
        outcome = {"ok": False, "error": "explicit_service_identity_unavailable", "runtime": active}

    # One automatic re-entry covers a transient timeout after the in-process
    # bounded retries have been exhausted.  A second failure is retained for
    # RESUME; raw sources remain untouched in both cases.
    if not outcome.get("ok") and outcome.get("error") == "close_day_retryable":
        retry = run_mlomega_command(["brainlive-resume-close-day", "--person-id", PERSON_ID, "--force"])
        outcome = {"ok": bool(retry.get("ok")) and _close_day_result_is_safe(retry.get("result")), "result": retry.get("result"), "automatic_resume": True, "retry": retry}

    if outcome.get("ok") and CLEANUP_AFTER_POST_STOP:
        cleanup_phone_media(cutoff_iso=cutoff_iso, reason="v18_8_close_day_cleanup_eligible")
    elif outcome.get("ok"):
        log("close-day completed; configured to retain phone raw media")
    else:
        log("cleanup skipped because V18.8 close-day did not complete with an eligible gate: " + json.dumps(outcome, ensure_ascii=False)[:1600])


@app.post("/session/stop")
async def session_stop(request: Request, x_mlomega_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    auth(x_mlomega_token)
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        pass
    payload.setdefault("event", "phone_capture_stop")
    payload.setdefault("received_at", utc_now())
    item_id, reused = enqueue("session_event", "session_stop.json", None, {"session_event": payload})
    log(f"session stop {payload}")
    launched = False
    if ALLOW_POST_STOP:
        threading.Thread(target=run_close_day, daemon=True, name="phone-close-day").start()
        launched = True
    return {
        "ok": True,
        "close_day_launched": launched,
        "post_stop_launched": launched,  # compatibility response field
        "queued_id": item_id,
        "reused": reused,
    }


@app.post("/cleanup-media")
def cleanup_media_endpoint(request: Request, x_mlomega_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    auth(x_mlomega_token)
    # Direct deletion is intentionally no longer allowed.  The same endpoint is
    # retained for compatibility but first requires the last V18.8 close-day
    # record to prove all deep stages and output manifests completed.
    gate = run_mlomega_command(["brainlive-close-day-status", "--person-id", PERSON_ID])
    result = gate.get("result") if isinstance(gate, dict) else None
    if not gate.get("ok") or not isinstance(result, dict) or not bool(result.get("cleanup_eligible")):
        raise HTTPException(status_code=409, detail={"error": "cleanup_not_eligible", "close_day": result})
    return cleanup_phone_media(cutoff_iso=utc_now(), reason="v18_8_manual_gate_eligible")


@app.post("/pump-now")
def pump_now(x_mlomega_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    auth(x_mlomega_token)
    n = 0
    while pump_one():
        n += 1
        if n >= 1000:
            break
    return {"ok": True, "pumped": n}
