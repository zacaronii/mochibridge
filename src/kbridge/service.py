"""Glue: poll sources -> filter -> resolve series -> desired logs -> sync worker."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import idmap
from .config import Config
from .db import DB, iso, parse_iso, utcnow
from .kechimochi import Kechimochi
from .filters import Decision
from .models import event_from_dict, event_to_dict
from .resolver import Resolver
from .sources.base import Polled, Source
from .sync import SyncStats, Syncer

log = logging.getLogger(__name__)


@dataclass
class PollStats:
    source: str
    since: datetime
    sessions: int = 0
    logged: int = 0
    changed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    waiting: int = 0


def local_date(cfg: Config, dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()


def event_meta(cfg: Config, p: Polled) -> dict:
    """Human-facing description of a session, kept next to logs/skips for reports and the web UI."""
    ev = p.event
    if ev is None:
        info = dict(p.info)
        if info.get("started_at"):
            info["date"] = local_date(cfg, datetime.fromisoformat(info["started_at"]))
        return info
    return {"library": ev.series.library_name, "series": ev.series.title, "note": ev.note,
            "date": local_date(cfg, ev.started_at), "started_at": ev.started_at.isoformat(),
            "minutes": round(ev.duration_seconds / 60, 1), "quality": ev.quality, "flags": ev.flags,
            "language": ev.language, "nagare_key": ev.nagare_key}


def desired_log(cfg: Config, polled: Polled) -> dict:
    ev = polled.event
    assert ev is not None
    local = ev.started_at.astimezone(ZoneInfo(cfg.timezone))
    return {
        "duration_minutes": max(1, round(ev.duration_seconds / 60)),
        "characters": 0,
        "date": local.date().isoformat(),
        "date_precision": "day",
        "activity_type": ev.series.activity_type,
        "notes": ev.note,
    }


class Bridge:
    def __init__(self, cfg: Config, db: DB, km: Kechimochi, sources: dict[str, Source]):
        self.cfg, self.db, self.km, self.sources = cfg, db, km, sources
        self.resolver = Resolver(db, km, cfg)
        self.syncer = Syncer(db, km, cfg, {n: s.fetch_cover for n, s in sources.items()})

    def _review_status(self, source: str, session_id: str, item_key: str) -> str | None:
        row = self.db.one("SELECT status FROM reviews WHERE source=? AND session_id=? AND item_key=?",
                          (source, session_id, item_key))
        return row["status"] if row else None

    def _decide(self, p: Polled, source: str) -> Decision:
        """The adapter's decision, adjusted by your review decisions and the long-session switch."""
        decision = p.decision
        if p.event is None:
            return decision
        status = self._review_status(source, p.session_id, p.item_key)
        if status == "approved":
            return Decision.ok()
        if status == "rejected":
            return Decision.skip("rejected")
        if (decision.keep and self.cfg.long_session_action == "review"
                and p.event.duration_seconds >= self.cfg.long_session_hours * 3600
                and not self.db.one("SELECT 1 FROM logs WHERE source=? AND session_id=? AND item_key=? AND log_id IS NOT NULL",
                                    (source, p.session_id, p.item_key))):
            return Decision.hold("long_session")
        return decision

    async def ingest(self, p: Polled, stats: PollStats) -> None:
        source = stats.source
        meta = json.dumps(event_meta(self.cfg, p), ensure_ascii=False)
        event_json = json.dumps(event_to_dict(p.event), ensure_ascii=False) if p.event else None
        decision = self._decide(p, source)
        if decision.review and p.event is not None:
            stats.skipped[f"held:{decision.reason}"] = stats.skipped.get(f"held:{decision.reason}", 0) + 1
            now = iso(utcnow())
            self.db.execute(
                "INSERT INTO reviews(source, session_id, item_key, reason, status, event_json, meta_json, created_at,"
                " updated_at) VALUES (?,?,?,?, 'pending', ?,?,?,?) ON CONFLICT(source, session_id, item_key) DO UPDATE"
                " SET reason=excluded.reason, event_json=excluded.event_json, meta_json=excluded.meta_json,"
                " updated_at=excluded.updated_at WHERE reviews.status='pending'",
                (source, p.session_id, p.item_key, decision.reason, event_json, meta, now, now))
            self.db.execute("DELETE FROM skips WHERE source=? AND session_id=? AND item_key=?",
                            (source, p.session_id, p.item_key))
            return
        if not decision.keep or p.event is None:
            if decision.wait:
                stats.waiting += 1
                return
            reason = decision.reason or "?"
            stats.skipped[reason] = stats.skipped.get(reason, 0) + 1
            self.db.execute(
                "INSERT INTO skips(source, session_id, item_key, reason, detail, seen_at, meta_json, event_json)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source, session_id, item_key) DO UPDATE SET"
                " reason=excluded.reason, detail=excluded.detail, seen_at=excluded.seen_at,"
                " meta_json=excluded.meta_json, event_json=excluded.event_json",
                (source, p.session_id, p.item_key, reason, p.detail, iso(utcnow()), meta, event_json))
            return
        ev = p.event
        status = await self.resolver.ensure(ev.series)
        if status == "ignored":
            stats.skipped["series_ignored"] = stats.skipped.get("series_ignored", 0) + 1
            return
        desired = json.dumps(desired_log(self.cfg, p), sort_keys=True, ensure_ascii=False)
        row = self.db.one("SELECT desired_json, final, meta_json FROM logs"
                          " WHERE source=? AND session_id=? AND item_key=?", (source, ev.session_id, ev.item_key))
        stats.logged += 1
        if row and row["desired_json"] == desired and bool(row["final"]) == ev.final and row["meta_json"] == meta:
            return
        stats.changed += 1
        self.db.execute(
            "INSERT INTO logs(source, session_id, item_key, series_key, desired_json, final, source_updated_at,"
            " started_at, updated_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source, session_id, item_key) DO UPDATE SET series_key=excluded.series_key,"
            " desired_json=excluded.desired_json, final=excluded.final,"
            " source_updated_at=excluded.source_updated_at, updated_at=excluded.updated_at,"
            " meta_json=excluded.meta_json",
            (source, ev.session_id, ev.item_key, ev.series.series_key, desired, int(ev.final),
             iso(ev.updated_at), iso(ev.started_at), iso(utcnow()), meta))
        self.db.execute("DELETE FROM skips WHERE source=? AND session_id=? AND item_key=?",
                        (source, ev.session_id, ev.item_key))

    # --------------------------------------------------------------- review
    def _stored_event(self, source: str, session_id: str, item_key: str):
        row = self.db.one("SELECT event_json FROM reviews WHERE source=? AND session_id=? AND item_key=?",
                          (source, session_id, item_key)) or \
            self.db.one("SELECT event_json FROM skips WHERE source=? AND session_id=? AND item_key=?",
                        (source, session_id, item_key))
        return event_from_dict(json.loads(row["event_json"])) if row and row["event_json"] else None

    async def approve(self, source: str, session_id: str, item_key: str) -> bool:
        """Log a held (or skipped) session. Later polls of the same session keep logging it."""
        ev = self._stored_event(source, session_id, item_key)
        if ev is None:
            return False
        now = iso(utcnow())
        self.db.execute(
            "INSERT INTO reviews(source, session_id, item_key, reason, status, event_json, created_at, updated_at,"
            " decided_at) VALUES (?,?,?, 'manual', 'approved', ?,?,?,?) ON CONFLICT(source, session_id, item_key)"
            " DO UPDATE SET status='approved', decided_at=excluded.decided_at",
            (source, session_id, item_key, json.dumps(event_to_dict(ev), ensure_ascii=False), now, now, now))
        self.resolver.invalidate()
        await self.ingest(Polled(session_id, item_key, ev, Decision.ok()), PollStats(source, utcnow()))
        self.db.event("info", source, f"approved {ev.series.title!r} {ev.note} ({round(ev.duration_seconds / 60)} min)")
        return True

    def reject(self, source: str, session_id: str, item_key: str) -> bool:
        row = self.db.one("SELECT * FROM reviews WHERE source=? AND session_id=? AND item_key=?",
                          (source, session_id, item_key))
        if row is None:
            return False
        now = iso(utcnow())
        self.db.execute("UPDATE reviews SET status='rejected', decided_at=? WHERE source=? AND session_id=? AND item_key=?",
                        (now, source, session_id, item_key))
        self.db.execute(
            "INSERT INTO skips(source, session_id, item_key, reason, detail, seen_at, meta_json, event_json)"
            " VALUES (?,?,?, 'rejected', '', ?,?,?) ON CONFLICT(source, session_id, item_key) DO UPDATE SET"
            " reason='rejected', seen_at=excluded.seen_at",
            (source, session_id, item_key, now, row["meta_json"], row["event_json"]))
        return True

    async def poll(self, name: str, since: datetime | None = None, now: datetime | None = None) -> PollStats:
        now = now or utcnow()
        if since is None:
            since = parse_iso(self.db.get_cursor(name, "polled_until"))
        if since is None:
            # First run: start from now. History is the backfill's job, not the live poller's.
            since = now
            self.db.event("info", name, "first poll: starting from now (no backfill)")
        stats = PollStats(name, since)
        polled = await self.sources[name].poll(since, now)
        stats.sessions = len(polled)
        self.resolver.invalidate()
        for p in polled:
            await self.ingest(p, stats)
        self.db.set_cursor(name, "polled_until", iso(now))
        return stats

    def apply_config(self, cfg: Config) -> None:
        """Swap in a new config (from the settings page) everywhere it's held; takes effect on the next poll.
        Mode and the set of running sources only change on restart."""
        cfg.mode = self.cfg.mode
        self.cfg = cfg
        self.resolver.cfg = cfg
        self.syncer.cfg = cfg
        for name, src in self.sources.items():
            src.cfg = cfg               # type: ignore[attr-defined]
            src.src = cfg.source(name)  # type: ignore[attr-defined]

    def remap(self, source: str, series_key: str, media_id: int | None, ignore: bool = False) -> None:
        """Point a series at another entry (media_id), back to "create a new entry" (None), or ignore it.
        Logs already in kechimochi are re-sent so they move with the series (PUT with the new media_id)."""
        if ignore:
            self.db.execute("UPDATE series_map SET status='ignored', link_method='override' WHERE source=? AND series_key=?",
                            (source, series_key))
            return
        if media_id is None:
            self.db.execute("UPDATE series_map SET status='pending_create', media_id=NULL, link_method=NULL,"
                            " meta_synced_hash=NULL, cover_done=0, written_keys='{}', attempts=0, next_attempt_at=NULL"
                            " WHERE source=? AND series_key=?", (source, series_key))
        else:
            self.db.execute("UPDATE series_map SET status='linked', media_id=?, link_method='override',"
                            " meta_synced_hash=NULL, cover_done=0, written_keys='{}', attempts=0, next_attempt_at=NULL"
                            " WHERE source=? AND series_key=?", (int(media_id), source, series_key))
        self.db.execute("UPDATE logs SET synced_hash=NULL, attempts=0, next_attempt_at=NULL WHERE source=? AND series_key=?",
                        (source, series_key))
        self.db.event("info", source, f"series {series_key} now → {('#' + str(media_id)) if media_id else 'new entry'}")

    def heartbeat(self, error: str | None = None) -> None:
        """Read by `kbridge health` (the container healthcheck)."""
        self.db.set_cursor("service", "heartbeat", iso(utcnow()))
        self.db.set_cursor("service", "last_error", error)

    async def sync(self) -> SyncStats:
        return await self.syncer.run_once()

    async def refresh_idmap(self, force: bool = False) -> int | None:
        return idmap.refresh(self.db, self.cfg.idmap_db, self.cfg.idmap_refresh_hours, force=force)

    async def run_forever(self, sync_seconds: int = 30) -> None:
        async def poll_loop(name: str) -> None:
            interval = self.cfg.source(name).poll_seconds
            while True:
                try:
                    st = await self.poll(name)
                    if st.changed:
                        log.info("%s: %d sessions, %d changed, skipped %s", name, st.sessions, st.changed, st.skipped)
                except Exception as e:  # keep polling; the error is in the feed
                    log.exception("%s poll failed", name)
                    self.db.event("error", name, f"poll failed: {e!r}")
                await asyncio.sleep(interval)

        async def sync_loop() -> None:
            while True:
                error = None
                try:
                    await self.km.version()          # kechimochi reachable? (also in dry-run)
                    st = await self.sync()
                    if st.details:
                        log.info("sync: %s", "; ".join(st.details[:20]))
                except Exception as e:
                    log.exception("sync failed")
                    error = repr(e)
                    self.db.event("error", None, f"sync failed: {e!r}")
                self.heartbeat(error)
                await asyncio.sleep(sync_seconds)

        async def idmap_loop() -> None:
            while True:
                try:
                    await self.refresh_idmap()
                except Exception as e:
                    log.exception("idmap refresh failed")
                    self.db.event("warn", None, f"idmap refresh failed: {e!r}")
                await asyncio.sleep(3600)

        tasks = [poll_loop(n) for n in self.sources] + [sync_loop(), idmap_loop()]
        await asyncio.gather(*tasks)


def parse_since(value: str, now: datetime) -> datetime:
    """'30d', '12h', '90m' or an ISO timestamp."""
    units = {"d": "days", "h": "hours", "m": "minutes"}
    if value and value[-1] in units and value[:-1].isdigit():
        return now - timedelta(**{units[value[-1]]: int(value[:-1])})
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)
