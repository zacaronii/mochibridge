"""Outbox worker: make kechimochi match the desired state in the DB.

The DB holds what *should* exist (series_map, logs). Each pass sends only the differences, so a crash or a
kechimochi restart just means the next pass picks up where this one stopped. In dry-run mode every
request is recorded in `ops` instead of being sent, and nothing in the DB is marked as synced.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable

from . import extra
from .config import Config
from .db import DB, iso, parse_iso, utcnow
from .kechimochi import Conflict, Kechimochi, KechimochiError, media_payload
from .resolver import stable_hash

log = logging.getLogger(__name__)

CoverFetcher = Callable[[str], Awaitable[tuple[bytes, str, str] | None]]
BACKOFF_MIN, BACKOFF_MAX = 30, 3600


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_MAX, BACKOFF_MIN * 2 ** max(0, attempts - 1)))


@dataclass
class SyncStats:
    media_created: int = 0
    media_updated: int = 0
    covers: int = 0
    logs_created: int = 0
    logs_updated: int = 0
    errors: int = 0
    dry_ops: int = 0
    details: list[str] = field(default_factory=list)


class Syncer:
    def __init__(self, db: DB, km: Kechimochi, cfg: Config, covers: dict[str, CoverFetcher] | None = None):
        self.db, self.km, self.cfg = db, km, cfg
        self.covers = covers or {}

    @property
    def dry(self) -> bool:
        return not self.cfg.live

    def _due(self, row) -> bool:
        nxt = parse_iso(row["next_attempt_at"])
        return nxt is None or nxt <= utcnow()

    def _dry_op(self, stats: SyncStats, method: str, path: str, body: Any, note: str) -> None:
        """Record a request we would have sent; identical requests are only recorded once."""
        body_json = json.dumps(body, ensure_ascii=False) if body is not None else None
        if self.db.one("SELECT 1 FROM ops WHERE mode='dry_run' AND method=? AND path=? AND body IS ?",
                       (method, path, body_json)):
            return
        self.db.record_op("dry_run", method, path, body, note)
        stats.dry_ops += 1
        stats.details.append(f"DRY {method} {path} {note}")

    def _fail(self, table: str, keys: dict[str, Any], row, err: Exception, stats: SyncStats, what: str) -> None:
        attempts = row["attempts"] + 1
        transient = isinstance(err, KechimochiError) and err.transient
        delay = backoff(attempts) if transient else timedelta(seconds=BACKOFF_MAX)
        where = " AND ".join(f"{k}=?" for k in keys)
        self.db.execute(f"UPDATE {table} SET attempts=?, next_attempt_at=?, last_error=? WHERE {where}",
                        (attempts, iso(utcnow() + delay), str(err)[:500], *keys.values()))
        stats.errors += 1
        level = "warn" if transient else "error"
        self.db.event(level, keys.get("source"), f"{what} failed (attempt {attempts}): {err}")
        log.warning("%s failed: %s", what, err)

    # ------------------------------------------------------------------ media
    async def sync_series(self, stats: SyncStats) -> None:
        for row in self.db.all("SELECT * FROM series_map WHERE status IN ('pending_create','linked')"):
            if not self._due(row):
                continue
            keys = {"source": row["source"], "series_key": row["series_key"]}
            try:
                if row["status"] == "pending_create":
                    await self._create(row, stats)
                else:
                    await self._fill(row, stats)
            except KechimochiError as e:
                self._fail("series_map", keys, row, e, stats, f"media sync for {row['title']!r}")

    async def _create(self, row, stats: SyncStats) -> None:
        desired = json.loads(row["desired_json"])
        fields = desired.pop("fields")
        payload = {**desired, "extra_data": json.dumps(fields, ensure_ascii=False)}
        if self.dry:
            self._dry_op(stats, "POST", "/api/media", media_payload({**payload, "id": None}),
                         f"create {row['title']!r}")
            return
        try:
            media_id, method = await self.km.create_media(payload), "created"
            stats.media_created += 1
        except Conflict:
            existing = await self.km.find_media(desired["title"], "")
            if existing is None:
                raise
            media_id, method = existing["id"], "conflict"
        self.db.execute(
            "UPDATE series_map SET media_id=?, status='linked', link_method=?, written_keys=?,"
            " meta_synced_hash=?, attempts=0, next_attempt_at=NULL, last_error=NULL"
            " WHERE source=? AND series_key=?",
            (media_id, method, json.dumps(fields if method == "created" else {}, ensure_ascii=False),
             stable_hash(row["desired_json"]) if method == "created" else None,
             row["source"], row["series_key"]))
        self.db.event("info", row["source"], f"{method} kechimochi entry #{media_id} {row['title']!r}")
        stats.details.append(f"{method} #{media_id} {row['title']}")
        row = self.db.one("SELECT * FROM series_map WHERE source=? AND series_key=?",
                          (row["source"], row["series_key"]))
        await self._fill(row, stats)

    async def _fill(self, row, stats: SyncStats) -> None:
        """Fill empty fields on a linked entry (description, extra_data keys, cover). Never overwrite."""
        desired = json.loads(row["desired_json"])
        written = json.loads(row["written_keys"] or "{}")
        meta_hash = stable_hash(row["desired_json"])
        want_cover = self.cfg.upload_covers and row["cover_ref"] and not row["cover_done"]
        if row["meta_synced_hash"] == meta_hash and not want_cover:
            return
        current = await self.km.get_media(row["media_id"])
        if current is None:
            self.db.execute("UPDATE series_map SET status='needs_mapping', media_id=NULL, last_error=?"
                            " WHERE source=? AND series_key=?",
                            (f"entry #{row['media_id']} no longer exists", row["source"], row["series_key"]))
            self.db.event("warn", row["source"],
                          f"entry #{row['media_id']} for {row['title']!r} was deleted; needs mapping")
            return

        if row["meta_synced_hash"] != meta_hash:
            updated = dict(current)
            if not (current.get("description") or "").strip() and desired.get("description"):
                updated["description"] = desired["description"]
            merged, new_written = extra.merge(extra.parse(current.get("extra_data")), desired["fields"], written)
            updated["extra_data"] = json.dumps(merged, ensure_ascii=False)
            changed = media_payload(updated) != media_payload(current)
            if changed and self.dry:
                self._dry_op(stats, "PUT", f"/api/media/{row['media_id']}", media_payload(updated),
                             f"fill empty fields on {current['title']!r}")
            elif changed:
                await self.km.put_media(updated)
                stats.media_updated += 1
                current = updated
            if not self.dry:
                self.db.execute("UPDATE series_map SET meta_synced_hash=?, written_keys=? WHERE source=? AND series_key=?",
                                (meta_hash, json.dumps(new_written, ensure_ascii=False), row["source"], row["series_key"]))

        if want_cover:
            if (current.get("cover_image") or "").strip():
                done = True   # user (or an importer) already set one; leave it
            elif self.dry:
                self._dry_op(stats, "POST", f"/api/covers/{row['media_id']}", None,
                             f"upload cover for {current['title']!r} from {row['cover_ref']}")
                done = False
            else:
                fetch = self.covers.get(row["source"])
                got = await fetch(row["cover_ref"]) if fetch else None
                if got:
                    await self.km.upload_cover(row["media_id"], *got)
                    stats.covers += 1
                done = True
            if done and not self.dry:
                self.db.execute("UPDATE series_map SET cover_done=1 WHERE source=? AND series_key=?",
                                (row["source"], row["series_key"]))
        if not self.dry:
            self.db.execute("UPDATE series_map SET attempts=0, next_attempt_at=NULL, last_error=NULL"
                            " WHERE source=? AND series_key=?", (row["source"], row["series_key"]))

    # ------------------------------------------------------------------- logs
    async def sync_logs(self, stats: SyncStats) -> None:
        rows = self.db.all(
            "SELECT l.*, s.media_id, s.status AS series_status, s.title AS series_title FROM logs l"
            " JOIN series_map s ON s.source=l.source AND s.series_key=l.series_key"
            " WHERE l.synced_hash IS NOT l.desired_json AND s.status IN ('linked','pending_create')"
            " AND l.retracted = 0 ORDER BY l.started_at")
        for row in rows:
            if not self._due(row):
                continue
            desired = json.loads(row["desired_json"])
            keys = {"source": row["source"], "session_id": row["session_id"], "item_key": row["item_key"]}
            if row["media_id"] is None:
                if self.dry:   # the entry would be created first; show the log against the new title
                    self._dry_op(stats, "POST", "/api/logs", {**desired, "media_id": f"<new {row['series_title']!r}>"},
                                 f"{row['series_title']!r} {desired['date']} {desired['duration_minutes']}m (entry not created yet)")
                continue
            payload = {**desired, "media_id": row["media_id"]}
            try:
                if self.dry:
                    method, path = ("POST", "/api/logs") if row["log_id"] is None else ("PUT", f"/api/logs/{row['log_id']}")
                    self._dry_op(stats, method, path, payload,
                                 f"{row['series_title']!r} {desired['date']} {desired['duration_minutes']}m")
                    continue
                if row["log_id"] is None:
                    log_id = await self.km.create_log(payload)
                    stats.logs_created += 1
                else:
                    log_id = row["log_id"]
                    try:
                        await self.km.put_log(log_id, payload)
                    except KechimochiError as e:
                        if not e.not_found:
                            raise
                        # Deleted by hand in kechimochi: respect that and stop tracking this session.
                        self.db.event("info", row["source"], f"log #{log_id} was deleted in kechimochi; not recreating")
                    else:
                        stats.logs_updated += 1
                self.db.execute(
                    "UPDATE logs SET log_id=?, synced_hash=desired_json, attempts=0, next_attempt_at=NULL,"
                    " last_error=NULL WHERE source=? AND session_id=? AND item_key=?",
                    (log_id, *keys.values()))
            except KechimochiError as e:
                self._fail("logs", keys, row, e, stats, f"log {row['session_id']} for {row['series_title']!r}")

    async def sync_retractions(self, stats: SyncStats) -> None:
        """Logs superseded by a better source: delete the one already in kechimochi, then forget the row."""
        for row in self.db.all("SELECT * FROM logs WHERE retracted = 1"):
            where = (row["source"], row["session_id"], row["item_key"])
            if row["log_id"] is not None:
                if self.dry:
                    self._dry_op(stats, "DELETE", f"/api/logs/{row['log_id']}", None, "superseded by a better source")
                    continue
                try:
                    await self.km.delete_log(row["log_id"])
                except KechimochiError as e:
                    if not e.not_found:
                        stats.errors += 1
                        self.db.event("warn", row["source"], f"couldn't remove superseded log #{row['log_id']}: {e}")
                        continue
                self.db.event("info", row["source"], f"removed log #{row['log_id']} (superseded by a better source)")
            self.db.execute("DELETE FROM logs WHERE source=? AND session_id=? AND item_key=?", where)

    async def run_once(self) -> SyncStats:
        stats = SyncStats()
        await self.sync_series(stats)
        await self.sync_retractions(stats)
        await self.sync_logs(stats)
        return stats
