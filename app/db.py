"""
Simple SQLite persistence for zzk.
Tables:
- channels: registered channels to monitor + auto record config
- recordings: history of recording sessions
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import orjson

from .paths import DB_PATH, ensure_runtime_dirs


@dataclass
class ChannelRow:
    id: int
    channel_id: str
    channel_name: str
    channel_image_url: Optional[str]
    auto_record: bool
    quality: str
    segment_minutes: int
    created_at: str
    updated_at: str


@dataclass
class RecordingRow:
    id: int
    channel_id: str
    channel_name: str
    started_at: str
    ended_at: Optional[str]
    status: str  # "recording" | "completed" | "error" | "stopped"
    base_path: str  # relative or absolute path to the recording dir
    playlist_path: Optional[str]
    segment_count: int
    total_duration: float
    quality: str
    error: Optional[str]


def init_db():
    ensure_runtime_dirs()
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE NOT NULL,
                channel_name TEXT NOT NULL,
                channel_image_url TEXT,
                auto_record INTEGER NOT NULL DEFAULT 1,
                quality TEXT NOT NULL DEFAULT 'best',
                segment_minutes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL,
                base_path TEXT NOT NULL,
                playlist_path TEXT,
                segment_count INTEGER DEFAULT 0,
                total_duration REAL DEFAULT 0,
                quality TEXT,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recordings_channel ON recordings(channel_id);
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recordings_started ON recordings(started_at);
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()


@contextmanager
def get_conn():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
    except sqlite3.OperationalError as exc:
        if "unable to open database file" in str(exc).lower():
            raise sqlite3.OperationalError(
                f"unable to open database file at {DB_PATH.resolve()} "
                "(check that the data directory exists and is writable; "
                "in Docker, ensure /app/data is mounted with uid 1000 or use the image entrypoint)"
            ) from exc
        raise
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------- Channels ----------------


def add_or_update_channel(
    channel_id: str,
    channel_name: str,
    channel_image_url: Optional[str] = None,
    auto_record: bool = True,
    quality: str = "best",
    segment_minutes: int = 0,
) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO channels (channel_id, channel_name, channel_image_url, auto_record, quality, segment_minutes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                channel_name=excluded.channel_name,
                channel_image_url=COALESCE(excluded.channel_image_url, channel_image_url),
                auto_record=excluded.auto_record,
                quality=excluded.quality,
                segment_minutes=excluded.segment_minutes,
                updated_at=excluded.updated_at
            """,
            (
                channel_id,
                channel_name,
                channel_image_url,
                1 if auto_record else 0,
                quality,
                int(segment_minutes),
                now,
                now,
            ),
        )
        conn.commit()
        # get id
        row = conn.execute(
            "SELECT id FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return int(row["id"]) if row else cur.lastrowid


def list_channels() -> list[ChannelRow]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM channels ORDER BY updated_at DESC"
        ).fetchall()
        return [
            ChannelRow(
                id=r["id"],
                channel_id=r["channel_id"],
                channel_name=r["channel_name"],
                channel_image_url=r["channel_image_url"],
                auto_record=bool(r["auto_record"]),
                quality=r["quality"],
                segment_minutes=r["segment_minutes"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]


def get_channel(channel_id: str) -> Optional[ChannelRow]:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if not r:
            return None
        return ChannelRow(
            id=r["id"],
            channel_id=r["channel_id"],
            channel_name=r["channel_name"],
            channel_image_url=r["channel_image_url"],
            auto_record=bool(r["auto_record"]),
            quality=r["quality"],
            segment_minutes=r["segment_minutes"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )


def delete_channel(channel_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
        conn.commit()


def update_channel_settings(
    channel_id: str,
    *,
    auto_record: Optional[bool] = None,
    quality: Optional[str] = None,
    segment_minutes: Optional[int] = None,
):
    sets = []
    args = []
    if auto_record is not None:
        sets.append("auto_record = ?")
        args.append(1 if auto_record else 0)
    if quality is not None:
        sets.append("quality = ?")
        args.append(quality)
    if segment_minutes is not None:
        sets.append("segment_minutes = ?")
        args.append(int(segment_minutes))
    if not sets:
        return
    sets.append("updated_at = ?")
    args.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    args.append(channel_id)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE channels SET {', '.join(sets)} WHERE channel_id = ?", args
        )
        conn.commit()


# ---------------- Recordings ----------------


def create_recording(
    channel_id: str,
    channel_name: str,
    base_path: str,
    quality: str,
    playlist_path: Optional[str] = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO recordings (channel_id, channel_name, started_at, status, base_path, playlist_path, quality, segment_count, total_duration)
            VALUES (?, ?, ?, 'recording', ?, ?, ?, 0, 0)
            """,
            (channel_id, channel_name, now, base_path, playlist_path, quality),
        )
        conn.commit()
        return cur.lastrowid


def update_recording(
    recording_id: int,
    *,
    status: Optional[str] = None,
    ended_at: Optional[str] = None,
    segment_count: Optional[int] = None,
    total_duration: Optional[float] = None,
    error: Optional[str] = None,
    playlist_path: Optional[str] = None,
    base_path: Optional[str] = None,
):
    sets = []
    args = []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if ended_at is not None:
        sets.append("ended_at = ?")
        args.append(ended_at)
    if segment_count is not None:
        sets.append("segment_count = ?")
        args.append(int(segment_count))
    if total_duration is not None:
        sets.append("total_duration = ?")
        args.append(float(total_duration))
    if error is not None:
        sets.append("error = ?")
        args.append(error)
    if playlist_path is not None:
        sets.append("playlist_path = ?")
        args.append(playlist_path)
    if base_path is not None:
        sets.append("base_path = ?")
        args.append(base_path)
    if not sets:
        return
    args.append(recording_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE recordings SET {', '.join(sets)} WHERE id = ?", args)
        conn.commit()


def list_recordings(limit: int = 200) -> list[RecordingRow]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM recordings ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_recording(r) for r in rows]


def list_active_recordings() -> list[RecordingRow]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM recordings WHERE status = 'recording' ORDER BY started_at DESC"
        ).fetchall()
        return [_row_to_recording(r) for r in rows]


def get_recording(recording_id: int) -> Optional[RecordingRow]:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM recordings WHERE id = ?", (recording_id,)
        ).fetchone()
        return _row_to_recording(r) if r else None


def delete_recording(recording_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        conn.commit()
        return cur.rowcount > 0


def get_latest_finished_recording(channel_id: str) -> Optional[RecordingRow]:
    """Most recent non-active recording for a channel (for same-broadcast resume)."""
    with get_conn() as conn:
        r = conn.execute(
            """
            SELECT * FROM recordings
            WHERE channel_id = ? AND status != 'recording'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (channel_id,),
        ).fetchone()
        return _row_to_recording(r) if r else None


def reopen_recording(recording_id: int) -> None:
    """Mark a finished recording row active again (resume same broadcast)."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE recordings
            SET status = 'recording', ended_at = NULL, error = NULL
            WHERE id = ?
            """,
            (recording_id,),
        )
        conn.commit()


def _row_to_recording(r: sqlite3.Row) -> RecordingRow:
    return RecordingRow(
        id=r["id"],
        channel_id=r["channel_id"],
        channel_name=r["channel_name"],
        started_at=r["started_at"],
        ended_at=r["ended_at"],
        status=r["status"],
        base_path=r["base_path"],
        playlist_path=r["playlist_path"],
        segment_count=r["segment_count"] or 0,
        total_duration=r["total_duration"] or 0.0,
        quality=r["quality"] or "best",
        error=r["error"],
    )


# ---------------- Settings (simple key-value) ----------------


def _ensure_settings_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def get_setting(key: str, default: Any = None) -> Any:
    with get_conn() as conn:
        _ensure_settings_table(conn)
        r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not r:
            return default
        try:
            val = r["value"]
            if isinstance(val, (bytes, bytearray, memoryview)):
                val = val.decode("utf-8")
            return orjson.loads(val)
        except Exception:
            return r["value"]


def set_setting(key: str, value: Any):
    with get_conn() as conn:
        _ensure_settings_table(conn)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                key,
                orjson.dumps(value).decode("utf-8")
                if not isinstance(value, str)
                else value,
            ),
        )
        conn.commit()


# Initialize on import
init_db()
