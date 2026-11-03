"""SQLite state: series mapping, desired logs (the outbox), cursors, skips, ops and the activity feed."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = [
    # v1
    """
    CREATE TABLE libraries (
        source TEXT NOT NULL, library_id TEXT NOT NULL, name TEXT NOT NULL, media_type TEXT,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, track_override INTEGER,
        PRIMARY KEY (source, library_id));
    CREATE TABLE series_map (
        source TEXT NOT NULL, series_key TEXT NOT NULL,
        media_id INTEGER, status TEXT NOT NULL,          -- pending_create | linked | needs_mapping | ignored
        link_method TEXT, title TEXT NOT NULL,
        desired_json TEXT NOT NULL,                       -- media payload the bridge wants (create / fill-empty)
        written_keys TEXT NOT NULL DEFAULT '[]',          -- extra_data keys the bridge has written
        meta_synced_hash TEXT, cover_ref TEXT, cover_done INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source, series_key));
    CREATE INDEX series_map_media ON series_map(media_id);
    CREATE TABLE logs (
        source TEXT NOT NULL, session_id TEXT NOT NULL, item_key TEXT NOT NULL,
        series_key TEXT NOT NULL, desired_json TEXT NOT NULL,
        log_id INTEGER, synced_hash TEXT, final INTEGER NOT NULL DEFAULT 0,
        source_updated_at TEXT, started_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source, session_id, item_key));
    CREATE TABLE skips (
        source TEXT NOT NULL, session_id TEXT NOT NULL, item_key TEXT NOT NULL,
        reason TEXT NOT NULL, detail TEXT, seen_at TEXT NOT NULL,
        PRIMARY KEY (source, session_id, item_key));
    CREATE TABLE cursors (source TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
        PRIMARY KEY (source, key));
    CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT NOT NULL,
        source TEXT, message TEXT NOT NULL);
    CREATE TABLE ops (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, mode TEXT NOT NULL,
        method TEXT NOT NULL, path TEXT NOT NULL, body TEXT, result TEXT);
    CREATE TABLE idmap (
        src_provider TEXT NOT NULL, src_id TEXT NOT NULL, src_scope TEXT,
        dst_provider TEXT NOT NULL, dst_id TEXT NOT NULL, dst_scope TEXT,
        src_range TEXT, dst_range TEXT);
    CREATE INDEX idmap_src ON idmap(src_provider, src_id);
    CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at TEXT);
    """,
    # v2: per-session descriptions for reports/UI, and title-match suggestions
    """
    ALTER TABLE logs ADD COLUMN meta_json TEXT;
    ALTER TABLE skips ADD COLUMN meta_json TEXT;
    CREATE TABLE suggestions (
        source TEXT NOT NULL, series_key TEXT NOT NULL, media_id INTEGER NOT NULL,
        title TEXT NOT NULL, score REAL NOT NULL, method TEXT NOT NULL,
        PRIMARY KEY (source, series_key, media_id));
    """,
    # v3: Jellyfin /Sessions samples (live watched-time measurement)
    """
    CREATE TABLE jf_samples (
        session_key TEXT NOT NULL, item_id TEXT NOT NULL, ts TEXT NOT NULL,
        position_ticks INTEGER, is_paused INTEGER NOT NULL, audio_index INTEGER, audio_lang TEXT);
    CREATE INDEX jf_samples_ts ON jf_samples(ts);
    CREATE INDEX jf_samples_session ON jf_samples(session_key, ts);
    """,
    # v4: migration journal (every destructive step, with the object as it was before)
    """
    CREATE TABLE migration_journal (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, step TEXT NOT NULL,
        target TEXT NOT NULL, before_json TEXT, result TEXT NOT NULL);
    CREATE INDEX migration_journal_target ON migration_journal(step, target);
    """,
    # v5: review queue (held sessions, with the full event so they can be logged later) and skipped events
    """
    CREATE TABLE reviews (
        source TEXT NOT NULL, session_id TEXT NOT NULL, item_key TEXT NOT NULL,
        reason TEXT NOT NULL, status TEXT NOT NULL,          -- pending | approved | rejected
        event_json TEXT, meta_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, decided_at TEXT,
        PRIMARY KEY (source, session_id, item_key));
    ALTER TABLE skips ADD COLUMN event_json TEXT;
    """,
    # v6: YouTube (extension sessions, share-sheet logs, history snapshots)
    """
    CREATE TABLE yt_sessions (
        session_id TEXT PRIMARY KEY, video_id TEXT NOT NULL, kind TEXT NOT NULL,   -- extension | share
        seconds REAL NOT NULL, position REAL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        payload_json TEXT, emitted_at TEXT);
    CREATE INDEX yt_sessions_video ON yt_sessions(video_id, started_at);
    CREATE TABLE yt_history (
        video_id TEXT PRIMARY KEY, percent REAL, position REAL, length REAL, first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, last_change TEXT, top_rank INTEGER);
    """,
    # v7: the viewing a history video is currently part of (progress within 45 min extends the same log)
    """
    ALTER TABLE yt_history ADD COLUMN cur_session TEXT;
    ALTER TABLE yt_history ADD COLUMN cur_seconds REAL;
    ALTER TABLE yt_history ADD COLUMN cur_started TEXT;
    ALTER TABLE yt_history ADD COLUMN cur_date TEXT;
    ALTER TABLE yt_history ADD COLUMN viewings INTEGER NOT NULL DEFAULT 0;
    """,
    # v8: logs replaced by a better source (e.g. a history estimate superseded by the extension's measurement)
    """
    ALTER TABLE logs ADD COLUMN retracted INTEGER NOT NULL DEFAULT 0;
    """,
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class DB:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The web UI and the pollers share one connection inside one event loop; the test client calls
        # from a helper thread, so allow that (access is never concurrent).
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(SCHEMA[version:], start=version + 1):
            with self.tx():
                for stmt in script.split(";"):
                    if stmt.strip():
                        self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version={i}")

    def tx(self):
        return _Tx(self.conn)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    # --- small helpers -------------------------------------------------
    def event(self, level: str, source: str | None, message: str) -> None:
        self.execute("INSERT INTO events(ts, level, source, message) VALUES (?,?,?,?)",
                     (iso(utcnow()), level, source, message))

    def get_cursor(self, source: str, key: str) -> str | None:
        row = self.one("SELECT value FROM cursors WHERE source=? AND key=?", (source, key))
        return row["value"] if row else None

    def set_cursor(self, source: str, key: str, value: str | None) -> None:
        self.execute("INSERT INTO cursors(source, key, value) VALUES (?,?,?) "
                     "ON CONFLICT(source, key) DO UPDATE SET value=excluded.value",
                     (source, key, value))

    def cache_get(self, key: str) -> Any | None:
        row = self.one("SELECT value, expires_at FROM cache WHERE key=?", (key,))
        if not row:
            return None
        if row["expires_at"] and parse_iso(row["expires_at"]) < utcnow():
            return None
        return json.loads(row["value"])

    def cache_set(self, key: str, value: Any, expires_at: datetime | None = None) -> None:
        self.execute("INSERT INTO cache(key, value, expires_at) VALUES (?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
                     (key, json.dumps(value, ensure_ascii=False), iso(expires_at)))

    def record_op(self, mode: str, method: str, path: str, body: Any, result: str) -> None:
        self.execute("INSERT INTO ops(ts, mode, method, path, body, result) VALUES (?,?,?,?,?,?)",
                     (iso(utcnow()), mode, method, path,
                      json.dumps(body, ensure_ascii=False) if body is not None else None, result))


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
