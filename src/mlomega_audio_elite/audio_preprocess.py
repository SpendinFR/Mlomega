from __future__ import annotations

"""Audio preprocessing for long 24/24 recordings.

Critical invariant for the project: the raw audio timeline is sacred.
Automatic flow never deletes silence by default because that would make proof
timestamps lie. Long recordings are split into working chunks while preserving a
mapping back to the original audio timeline.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, init_db, upsert
from .utils import json_dumps, json_loads, now_iso, sha256_file, stable_id


def ensure_audio_preprocess_schema() -> None:
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS audio_preprocess_runs(
                run_id TEXT PRIMARY KEY,
                source_audio_path TEXT NOT NULL,
                source_sha256 TEXT,
                output_dir TEXT NOT NULL,
                silence_removed INTEGER NOT NULL,
                max_chunk_seconds INTEGER NOT NULL,
                status TEXT NOT NULL,
                command_json TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio_segments(
                segment_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                source_audio_path TEXT NOT NULL,
                segment_path TEXT NOT NULL,
                segment_index INTEGER NOT NULL,
                start_s REAL,
                end_s REAL,
                duration_s REAL,
                silence_policy_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio_chunk_groups(
                group_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                source_audio_path TEXT NOT NULL,
                source_sha256 TEXT,
                chunk_count INTEGER DEFAULT 0,
                preserves_original_timestamps INTEGER NOT NULL DEFAULT 1,
                timestamp_policy TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio_timestamp_maps(
                map_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                segment_path TEXT NOT NULL,
                segment_start_s REAL NOT NULL DEFAULT 0,
                segment_end_s REAL,
                original_start_s REAL,
                original_end_s REAL,
                mapping_status TEXT NOT NULL,
                warning TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio_chunk_conversation_links(
                link_id TEXT PRIMARY KEY,
                run_id TEXT,
                group_id TEXT,
                segment_id TEXT,
                segment_path TEXT,
                conversation_id TEXT NOT NULL,
                original_start_s REAL,
                original_end_s REAL,
                timestamp_offset_applied INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        # Add columns for already-created old databases. SQLite has no IF NOT EXISTS
        # for ADD COLUMN, so failures mean the column already exists.
        for stmt in [
            "ALTER TABLE audio_preprocess_runs ADD COLUMN preserves_original_timestamps INTEGER DEFAULT 1",
            "ALTER TABLE audio_preprocess_runs ADD COLUMN timestamp_policy TEXT DEFAULT 'original_chunked_no_silence_deleted'",
        ]:
            try:
                con.execute(stmt)
            except Exception:
                pass
        con.commit()


def _require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg introuvable. Installe FFmpeg et vérifie qu'il est dans le PATH.")
    return exe


def _probe_duration(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    cmd = [exe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    try:
        raw = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout
        data = json.loads(raw or "{}")
        value = data.get("format", {}).get("duration")
        return float(value) if value is not None else None
    except Exception:
        return None


def preprocess_audio(
    audio_path: Path,
    *,
    remove_silence: bool = False,
    max_chunk_seconds: int = 900,
    silence_threshold_db: str = "-40dB",
    min_silence_seconds: float = 1.2,
) -> dict[str, Any]:
    """Create chunked working files while preserving original time by default.

    If remove_silence=True, the run is explicitly marked unsafe unless a future
    exact silence-remap engine is implemented. The direct 24/24 flow calls this
    with remove_silence=False.
    """
    ensure_audio_preprocess_schema()
    ffmpeg = _require_ffmpeg()
    audio_path = Path(audio_path).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    settings = get_settings()
    out_dir = settings.root_dir / "preprocessed_audio" / audio_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    now = now_iso()
    source_hash = sha256_file(audio_path)
    run_id = stable_id("audprep", str(audio_path), source_hash, now)
    group_id = stable_id("audgroup", run_id, str(audio_path), source_hash)
    working = audio_path
    commands: list[list[str]] = []
    timestamp_policy = "original_chunked_no_silence_deleted"
    preserves_original_timestamps = 1
    try:
        if remove_silence:
            timestamp_policy = "unsafe_silence_removed_without_exact_remap"
            preserves_original_timestamps = 0
            working = out_dir / "silence_trimmed.wav"
            cmd = [
                ffmpeg, "-y", "-i", str(audio_path),
                "-af", f"silenceremove=start_periods=1:start_duration={min_silence_seconds}:start_threshold={silence_threshold_db}:stop_periods=-1:stop_duration={min_silence_seconds}:stop_threshold={silence_threshold_db}",
                str(working),
            ]
            commands.append(cmd)
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pattern = out_dir / "chunk_%04d.wav"
        cmd = [ffmpeg, "-y", "-i", str(working), "-f", "segment", "-segment_time", str(int(max_chunk_seconds)), "-reset_timestamps", "1", str(pattern)]
        commands.append(cmd)
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chunks = sorted(out_dir.glob("chunk_*.wav"))
        with connect() as con:
            upsert(con, "audio_preprocess_runs", {
                "run_id": run_id,
                "source_audio_path": str(audio_path),
                "source_sha256": source_hash,
                "output_dir": str(out_dir),
                "silence_removed": 1 if remove_silence else 0,
                "max_chunk_seconds": int(max_chunk_seconds),
                "status": "ok",
                "command_json": json_dumps(commands),
                "error_text": None,
                "created_at": now,
                "preserves_original_timestamps": preserves_original_timestamps,
                "timestamp_policy": timestamp_policy,
            }, "run_id")
            upsert(con, "audio_chunk_groups", {
                "group_id": group_id,
                "run_id": run_id,
                "source_audio_path": str(audio_path),
                "source_sha256": source_hash,
                "chunk_count": len(chunks),
                "preserves_original_timestamps": preserves_original_timestamps,
                "timestamp_policy": timestamp_policy,
                "created_at": now,
            }, "group_id")
            for idx, chunk in enumerate(chunks):
                duration = _probe_duration(chunk)
                original_start = float(idx * max_chunk_seconds) if not remove_silence else None
                original_end = original_start + duration if original_start is not None and duration is not None else None
                segment_id = stable_id("audseg", run_id, idx, str(chunk))
                upsert(con, "audio_segments", {
                    "segment_id": segment_id,
                    "run_id": run_id,
                    "source_audio_path": str(audio_path),
                    "segment_path": str(chunk),
                    "segment_index": idx,
                    "start_s": original_start,
                    "end_s": original_end,
                    "duration_s": duration,
                    "silence_policy_json": json_dumps({"remove_silence": remove_silence, "threshold": silence_threshold_db, "min_silence_seconds": min_silence_seconds, "timestamp_policy": timestamp_policy}),
                    "created_at": now,
                }, "segment_id")
                upsert(con, "audio_timestamp_maps", {
                    "map_id": stable_id("audmap", segment_id),
                    "run_id": run_id,
                    "segment_id": segment_id,
                    "segment_path": str(chunk),
                    "segment_start_s": 0.0,
                    "segment_end_s": duration,
                    "original_start_s": original_start,
                    "original_end_s": original_end,
                    "mapping_status": "exact_offset" if not remove_silence else "unsafe_missing_exact_silence_remap",
                    "warning": None if not remove_silence else "Silence was removed; original timestamps cannot be trusted without a full silence-remap table.",
                    "created_at": now,
                }, "map_id")
            con.commit()
        return {"status": "ok", "run_id": run_id, "group_id": group_id, "chunks": [str(p) for p in chunks], "output_dir": str(out_dir), "timestamp_policy": timestamp_policy, "preserves_original_timestamps": bool(preserves_original_timestamps)}
    except Exception as exc:
        with connect() as con:
            upsert(con, "audio_preprocess_runs", {
                "run_id": run_id,
                "source_audio_path": str(audio_path),
                "source_sha256": source_hash,
                "output_dir": str(out_dir),
                "silence_removed": 1 if remove_silence else 0,
                "max_chunk_seconds": int(max_chunk_seconds),
                "status": "error",
                "command_json": json_dumps(commands),
                "error_text": str(exc)[:2000],
                "created_at": now,
                "preserves_original_timestamps": preserves_original_timestamps,
                "timestamp_policy": timestamp_policy,
            }, "run_id")
            con.commit()
        raise


def apply_audio_segment_mapping(conversation_id: str, segment_path: Path) -> dict[str, Any]:
    """Link an ingested chunk conversation to the original audio time axis."""
    ensure_audio_preprocess_schema()
    segment_path = Path(segment_path).expanduser().resolve()
    now = now_iso()
    with connect() as con:
        row = con.execute("SELECT * FROM audio_timestamp_maps WHERE segment_path=? ORDER BY created_at DESC LIMIT 1", (str(segment_path),)).fetchone()
        if row is None:
            return {"conversation_id": conversation_id, "segment_path": str(segment_path), "status": "no_mapping"}
        m = dict(row)
        seg = con.execute("SELECT * FROM audio_segments WHERE segment_id=?", (m["segment_id"],)).fetchone()
        group = con.execute("SELECT * FROM audio_chunk_groups WHERE run_id=?", (m["run_id"],)).fetchone()
        original_start = m.get("original_start_s")
        original_end = m.get("original_end_s")
        offset = float(original_start or 0.0)
        mapping_status = str(m.get("mapping_status") or "unknown")
        applied = 0
        if mapping_status == "exact_offset":
            for table, id_col in [("turns", "turn_id"), ("source_spans", "span_id")]:
                rows = [dict(r) for r in con.execute(f"SELECT * FROM {table} WHERE conversation_id=?", (conversation_id,))]
                for r in rows:
                    meta = json_loads(r.get("metadata_json"), {}) or {}
                    if meta.get("original_audio_time_applied"):
                        continue
                    updates: dict[str, Any] = {"metadata_json": json_dumps({**meta, "original_audio_time_applied": True, "chunk_segment_path": str(segment_path), "chunk_original_offset_s": offset, "timestamp_policy": mapping_status})}
                    if r.get("start_s") is not None:
                        updates["start_s"] = float(r["start_s"]) + offset
                    if r.get("end_s") is not None:
                        updates["end_s"] = float(r["end_s"]) + offset
                    set_clause = ", ".join([f"{k}=?" for k in updates])
                    con.execute(f"UPDATE {table} SET {set_clause} WHERE {id_col}=?", (*updates.values(), r[id_col]))
                    applied += 1
        raw = con.execute("SELECT raw_json FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        raw_json = json_loads(raw["raw_json"], {}) if raw else {}
        raw_json["audio_chunk_mapping"] = {"segment_path": str(segment_path), "original_start_s": original_start, "original_end_s": original_end, "mapping_status": mapping_status, "run_id": m.get("run_id")}
        con.execute("UPDATE conversations SET raw_json=? WHERE conversation_id=?", (json_dumps(raw_json), conversation_id))
        link_id = stable_id("audconvlink", conversation_id, str(segment_path), m.get("run_id"))
        upsert(con, "audio_chunk_conversation_links", {
            "link_id": link_id,
            "run_id": m.get("run_id"),
            "group_id": group["group_id"] if group else None,
            "segment_id": m.get("segment_id"),
            "segment_path": str(segment_path),
            "conversation_id": conversation_id,
            "original_start_s": original_start,
            "original_end_s": original_end,
            "timestamp_offset_applied": 1 if applied else 0,
            "created_at": now,
        }, "link_id")
        con.commit()
    return {"conversation_id": conversation_id, "segment_path": str(segment_path), "status": "linked", "mapping_status": mapping_status, "rows_offset_applied": applied}
