"""YouTube watch-history reader (unofficial): the account's history page, read with its cookies every few minutes.

YouTube only exposes how far into each video you got (the red "watched" bar), so viewings are estimated from how
that bar moves between polls. Guards keep the estimate honest (capped at the time that actually passed).
The deploy README describes the accuracy to expect."""

from __future__ import annotations

import http.cookiejar
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .. import filters
from ..config import Config
from ..db import DB, iso, parse_iso, utcnow
from ..models import ActivityEvent
from .base import Polled
from .youtube import HEADERS, LIBRARY, YouTubeMeta, channel_meta, language_of

log = logging.getLogger(__name__)
_INITIAL = re.compile(r"var ytInitialData\s*=\s*(\{.+?\});\s*</script>", re.S)
_LOGGED_IN = re.compile(r'"LOGGED_IN"\s*:\s*(true|false)')
VIEWING_GAP = timedelta(minutes=45)      # later progress within this continues the same viewing (same log)
MIN_BAR_PERCENT = 10                     # YouTube shows at least 10% once a video is started (e.g. 2 min of a
                                         # 29-min video shows as 10%)
MIN_BAR_MAX = timedelta(minutes=10)      # cap for a "somewhere under 10%" first viewing
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WEEKDAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}


class SignedOut(Exception):
    pass


class FormatChanged(Exception):
    pass


@dataclass
class HistoryItem:
    video_id: str
    title: str
    channel_id: str | None
    channel: str | None
    length: float | None
    percent: float | None
    section: str | None
    short: bool = False


# ------------------------------------------------------------------ parsing
def _length(text: str | None) -> float | None:
    if not text or not re.fullmatch(r"\d+(:\d{2}){1,2}", text.strip()):
        return None
    secs = 0
    for part in text.strip().split(":"):
        secs = secs * 60 + int(part)
    return float(secs)


def _texts(o: Any):
    """Every display string under a node (YouTube puts text under `content`, `simpleText` or `text`)."""
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("content", "simpleText", "text") and isinstance(v, str):
                yield v
            else:
                yield from _texts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _texts(v)


def _find(o: Any, key: str):
    if isinstance(o, dict):
        if key in o:
            yield o[key]
        for v in o.values():
            yield from _find(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _find(v, key)


def _lockup(lv: dict[str, Any], section: str | None) -> HistoryItem | None:
    vid = lv.get("contentId")
    if not vid or "VIDEO" not in str(lv.get("contentType", "VIDEO")).upper():
        return None
    meta = (lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    title = (meta.get("title") or {}).get("content") or next(_texts(meta.get("title")), "")
    channel_id = next((b.get("browseId") for b in _find(meta, "browseEndpoint")
                       if str(b.get("browseId", "")).startswith("UC")), None)
    rows = list(_find(meta.get("metadata") or {}, "metadataRows"))
    channel = next(_texts(rows[0][0]), None) if rows and rows[0] else None
    image = lv.get("contentImage") or {}
    length = next((l for l in (_length(t) for t in _texts(list(_find(image, "thumbnailBadgeViewModel")))) if l), None)
    percent = next((float(p["startPercent"]) for p in _find(image, "thumbnailOverlayProgressBarViewModel")
                    if isinstance(p, dict) and "startPercent" in p), None)
    return HistoryItem(vid, title.strip(), channel_id, channel.strip() if channel else None, length, percent, section)


def _video_renderer(vr: dict[str, Any], section: str | None) -> HistoryItem | None:
    """The pre-2025 format, kept as a fallback."""
    vid = vr.get("videoId")
    if not vid:
        return None
    channel_id = next((b.get("browseId") for b in _find(vr.get("ownerText") or vr.get("longBylineText") or {},
                                                        "browseEndpoint") if b.get("browseId")), None)
    percent = next((float(p["percentDurationWatched"]) for p in _find(vr, "thumbnailOverlayResumePlaybackRenderer")
                    if "percentDurationWatched" in p), None)
    return HistoryItem(vid, next(_texts(vr.get("title")), ""), channel_id,
                       next(_texts(vr.get("ownerText") or vr.get("longBylineText")), None),
                       _length(next(_texts(vr.get("lengthText")), None)), percent, section)


def _short(o: dict[str, Any], section: str | None) -> HistoryItem | None:
    vid = next((w.get("videoId") for w in _find(o, "reelWatchEndpoint") if w.get("videoId")), None) \
        or o.get("videoId") or o.get("entityId", "").replace("shorts-shelf-item-", "") or None
    if not vid or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return None
    return HistoryItem(vid, next(_texts(o.get("overlayMetadata") or o.get("headline") or {}), ""), None, None,
                       None, None, section, short=True)


def parse_history(data: dict[str, Any]) -> list[HistoryItem]:
    """All entries, newest first, each with the day section it's listed under."""
    out: list[HistoryItem] = []

    def walk(o: Any, section: str | None) -> None:
        if isinstance(o, dict):
            if "itemSectionRenderer" in o:
                isr = o["itemSectionRenderer"]
                head = next(_texts(isr.get("header") or {}), None)
                for c in isr.get("contents") or []:
                    walk(c, head or section)
                return
            for key, fn in (("lockupViewModel", _lockup), ("videoRenderer", _video_renderer),
                            ("shortsLockupViewModel", _short), ("reelItemRenderer", _short)):
                if key in o and isinstance(o[key], dict):
                    item = fn(o[key], section)
                    if item:
                        out.append(item)
                    return
            for v in o.values():
                walk(v, section)
        elif isinstance(o, list):
            for v in o:
                walk(v, section)

    walk(data, None)
    return out


def section_date(header: str | None, today: date) -> date:
    """'Today', 'Yesterday', 'Wednesday', 'Oct 25', 'Oct 25, 2025' -> a date (today if unrecognised)."""
    h = (header or "").strip().lower()
    if not h or h == "today":
        return today
    if h == "yesterday":
        return today - timedelta(days=1)
    if h in WEEKDAYS:
        back = (today.weekday() - WEEKDAYS[h]) % 7 or 7
        return today - timedelta(days=back)
    m = re.fullmatch(r"([a-z]{3})[a-z]*\.? (\d{1,2})(?:, (\d{4}))?", h)
    if m and m.group(1) in MONTHS:
        year = int(m.group(3)) if m.group(3) else today.year
        d = date(year, MONTHS[m.group(1)], int(m.group(2)))
        if not m.group(3) and d > today:
            d = date(year - 1, d.month, d.day)
        return d
    return today


def extract(html: str) -> dict[str, Any]:
    m = _LOGGED_IN.search(html)
    if m and m.group(1) == "false":
        raise SignedOut("YouTube says the cookies are signed out")
    data = _INITIAL.search(html)
    if not data:
        raise FormatChanged("no ytInitialData on the history page")
    try:
        return json.loads(data.group(1))
    except ValueError as e:
        raise FormatChanged(f"ytInitialData is not JSON: {e}") from e


# ------------------------------------------------------------------ reader
class HistoryReader:
    def __init__(self, cfg: Config, db: DB, meta: YouTubeMeta, cookies_path: Path,
                 client: httpx.AsyncClient | None = None):
        self.cfg, self.db, self.meta, self.cookies_path = cfg, db, meta, cookies_path
        self.client = client

    @property
    def src(self):
        return self.cfg.source("youtube")

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=float(self.src.extra.get("history_poll_minutes", 10)))

    def _cookies(self) -> httpx.Cookies:
        jar = http.cookiejar.MozillaCookieJar(str(self.cookies_path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as e:
            raise SignedOut(f"can't read the cookies file: {e}") from e
        cookies = httpx.Cookies()
        for c in jar:
            cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        return cookies

    async def fetch(self) -> list[HistoryItem]:
        if not self.cookies_path.exists():
            raise SignedOut("no cookies uploaded yet")
        headers = dict(HEADERS, **{"Accept-Language": "en"})
        auth_user = self.db.get_cursor("youtube", "auth_user")
        if auth_user:
            headers["X-Goog-AuthUser"] = auth_user
        client = self.client or httpx.AsyncClient(timeout=30, follow_redirects=True)
        try:
            r = await client.get("https://www.youtube.com/feed/history", params={"hl": "en"},
                                 headers=headers, cookies=self._cookies())
        finally:
            if self.client is None:
                await client.aclose()
        if "accounts.google.com" in str(r.url.host) or r.status_code in (401, 403):
            raise SignedOut("redirected to sign-in")
        r.raise_for_status()
        items = parse_history(extract(r.text))
        if not items:
            raise FormatChanged("history page parsed but no videos found")
        return items

    def _health(self, state: str, error: str | None, now: datetime) -> None:
        prev = self.db.get_cursor("youtube", "history_state")
        self.db.set_cursor("youtube", "history_state", state)
        self.db.set_cursor("youtube", "history_checked", iso(now))
        self.db.set_cursor("youtube", "history_error", error)
        if state == "ok":
            self.db.set_cursor("youtube", "history_ok", iso(now))
        if state != prev:
            self.db.event("info" if state == "ok" else "error", "youtube",
                          f"history reader: {state}" + (f" ({error})" if error else ""))

    async def poll(self, since: datetime, now: datetime) -> list[Polled]:
        checked = parse_iso(self.db.get_cursor("youtube", "history_checked"))
        if checked and now - checked < self.interval:
            return []
        try:
            items = await self.fetch()
        except SignedOut as e:
            self._health("signed_out", str(e), now)
            return []
        except FormatChanged as e:
            self._health("format_changed", str(e), now)
            return []
        except httpx.HTTPError as e:
            self._health("error", repr(e)[:200], now)
            return []
        out = await self.process(items, now)
        self._health("ok", None, now)
        return out

    # ------------------------------------------------------------- snapshot -> viewings
    async def process(self, items: list[HistoryItem], now: datetime) -> list[Polled]:
        tz = ZoneInfo(self.cfg.timezone)
        today = now.astimezone(tz).date()
        last_ok = parse_iso(self.db.get_cursor("youtube", "history_ok"))
        baseline = self.db.get_cursor("youtube", "history_baselined") is None
        elapsed_cap = (now - last_ok).total_seconds() if last_ok else self.interval.total_seconds()
        seen: set[str] = set()
        out: list[Polled] = []
        for rank, it in enumerate(items):
            if it.video_id in seen:          # listed once per day watched; the newest occurrence is the state
                continue
            seen.add(it.video_id)
            row = self.db.one("SELECT * FROM yt_history WHERE video_id=?", (it.video_id,))
            pos = (it.percent / 100 * it.length) if it.percent is not None and it.length else None
            if baseline:                     # first run: remember where everything is, log nothing
                self._store(it, pos, rank, now, row, changed=False)
                continue
            delta, flags, hold = 0.0, [], None
            if it.short:
                if row is None:      # the history shelf has no length; _short_event looks up the real one
                    delta, flags = (it.length or 30.0), ["short"]
            elif row is None:
                delta = pos or 0.0
                if it.percent is not None and it.percent <= MIN_BAR_PERCENT:
                    # Watched somewhere between a few seconds and 10%. history_min_bar: half (default) logs 5%,
                    # full logs 10%, review holds it, ignore skips it; always capped (elapsed time, 10 min).
                    mode = self.src.extra.get("history_min_bar", "half")
                    flags = ["min_bar"]
                    if mode == "ignore":
                        delta = 0.0
                    elif mode == "review":
                        hold = "history_min_bar"
                    elif mode == "half":
                        delta = delta / 2
                    if mode != "ignore":
                        delta = min(delta, MIN_BAR_MAX.total_seconds())
            elif pos is not None and row["position"] is not None:
                if self._min_bar_resolved(it, row):
                    # First seen at YouTube's 10% minimum, now past it: the real position is finally known, so the
                    # viewing counts up to it (from the start) instead of the guess plus the growth beyond 10%.
                    delta = pos - self._credited(row, now, section_date(it.section, today))
                elif pos > row["position"] + 5:
                    delta = pos - row["position"]
                elif pos < row["position"] - 60:
                    delta, flags = pos, ["rewatch"]
                elif (it.percent or 0) >= 95 and (row["percent"] or 0) >= 95 and rank == 0 and row["top_rank"] != 0:
                    delta, flags, hold = (it.length or 0.0), ["rewatch"], "history_rewatch"
            if delta > 0 and not hold:
                if delta > elapsed_cap:
                    flags.append("capped")
                delta = min(delta, elapsed_cap)
            if delta <= 0:
                self._store(it, pos, rank, now, row, changed=False)
                continue
            p = await self._viewing(it, delta, flags, hold, now, today, row)
            self._store(it, pos, rank, now, self.db.one("SELECT * FROM yt_history WHERE video_id=?", (it.video_id,)),
                        changed=True)
            if p:
                out.append(p)
        if baseline:
            self.db.set_cursor("youtube", "history_baselined", iso(now))
            self.db.event("info", "youtube", f"history reader: baseline of {len(seen)} videos recorded (nothing logged)")
        return out

    @staticmethod
    def _continues(row, now: datetime, day: date) -> bool:
        """Is a change to this video part of its current viewing (one log that grows)?"""
        last_change = parse_iso(row["last_change"]) if row is not None and row["last_change"] else None
        return bool(row is not None and row["cur_session"] and last_change and now - last_change <= VIEWING_GAP
                    and (row["cur_date"] or "") == day.isoformat())

    @staticmethod
    def _min_bar_resolved(it: HistoryItem, row) -> bool:
        """The video's only viewing began at the 10% minimum bar, and the bar has now passed it."""
        return (row["viewings"] == 1 and (row["cur_session"] or "").endswith(":1")
                and row["percent"] is not None and row["percent"] <= MIN_BAR_PERCENT
                and it.percent is not None and it.percent > MIN_BAR_PERCENT)

    def _credited(self, row, now: datetime, day: date) -> float:
        """Seconds of this video already counted: the current viewing grows to the new total; a finished one
        counts only if it was logged (a too-short guess that was skipped is folded into the new viewing)."""
        if self._continues(row, now, day):
            return float(row["cur_seconds"] or 0)
        logged = self.db.one("SELECT 1 FROM logs WHERE source='youtube' AND session_id=? AND retracted=0",
                             (f"yt:{row['cur_session']}",))
        return float(row["cur_seconds"] or 0) if logged else 0.0

    def _store(self, it: HistoryItem, pos: float | None, rank: int, now: datetime, row, changed: bool) -> None:
        self.db.execute(
            "INSERT INTO yt_history(video_id, percent, position, length, first_seen, last_seen, last_change, top_rank)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET percent=excluded.percent,"
            " position=excluded.position, length=coalesce(excluded.length, yt_history.length),"
            " last_seen=excluded.last_seen, top_rank=excluded.top_rank,"
            " last_change=CASE WHEN ? THEN excluded.last_change ELSE yt_history.last_change END",
            (it.video_id, it.percent, pos, it.length, iso(now), iso(now), iso(now), rank, int(changed)))

    async def _viewing(self, it: HistoryItem, delta: float, flags: list[str], hold: str | None, now: datetime,
                       today: date, row) -> Polled | None:
        tz = ZoneInfo(self.cfg.timezone)
        day = section_date(it.section, today)
        # Continue the current viewing (one log that grows) or start a new one.
        if self._continues(row, now, day) and "rewatch" not in flags:
            session, seconds, started = row["cur_session"], float(row["cur_seconds"] or 0) + delta, \
                parse_iso(row["cur_started"])
        else:
            n = (row["viewings"] or 0) + 1 if row is not None else 1
            session, seconds = f"yth:{it.video_id}:{n}", delta
            started = now - timedelta(seconds=delta) if day == today else \
                datetime.combine(day, time(12), tz).astimezone(now.tzinfo)
            self.db.execute("INSERT INTO yt_history(video_id, first_seen, last_seen, viewings) VALUES (?,?,?,1)"
                            " ON CONFLICT(video_id) DO UPDATE SET viewings=coalesce(yt_history.viewings, 0)+1",
                            (it.video_id, iso(now), iso(now)))
        if it.short:
            return await self._short_event(it, now, day)
        self.db.execute("UPDATE yt_history SET cur_session=?, cur_seconds=?, cur_started=?, cur_date=? WHERE video_id=?",
                        (session, seconds, iso(started), day.isoformat(), it.video_id))
        # Measured (extension) or your own minutes (share sheet) win over this estimate.
        if self.db.one("SELECT 1 FROM yt_sessions WHERE video_id=? AND updated_at >= ?",
                       (it.video_id, iso(started - timedelta(minutes=30)))):
            return None
        return await self._event(f"yt:{session}", it, seconds, started, now, flags, hold)

    async def _short_event(self, it: HistoryItem, now: datetime, day: date) -> Polled | None:
        info = await self.meta.video(it.video_id)
        if not info.get("channel_id"):
            return None
        key = f"youtube:shorts:{info['channel_id']}:{day.isoformat()}"
        agg = self.db.cache_get(key) or {"seconds": 0.0, "videos": [], "started": iso(now)}
        if it.video_id not in agg["videos"]:
            agg["videos"].append(it.video_id)
            agg["seconds"] += float(info.get("length") or it.length or 30)
        self.db.cache_set(key, agg, now + timedelta(days=3))
        item = HistoryItem(it.video_id, f"Shorts ×{len(agg['videos'])}", info["channel_id"], info.get("channel"),
                           None, None, it.section, short=True)
        return await self._event(f"yts:{info['channel_id']}:{day.isoformat()}", item, agg["seconds"],
                                 parse_iso(agg["started"]), now, ["short"], None, info)

    async def _event(self, session_id: str, it: HistoryItem, seconds: float, started: datetime, now: datetime,
                     flags: list[str], hold: str | None, info: dict[str, Any] | None = None) -> Polled:
        info = info or await self.meta.video(it.video_id, {"title": it.title, "channel_id": it.channel_id,
                                                            "channel": it.channel, "length": it.length})
        meta = channel_meta(info, await self.meta.channel(info["channel_id"])) if info.get("channel_id") else None
        if meta is None:
            return Polled(session_id, it.video_id, None, filters.Decision.skip("metadata_missing"), it.title)
        note = it.title if it.short else f"{it.title} · https://youtu.be/{it.video_id}"
        language = language_of(info.get("title") or it.title, info.get("channel") or it.channel)
        event = ActivityEvent(source="youtube", session_id=session_id, item_key=it.video_id if not it.short else "shorts",
                              series=meta, note=note, started_at=started, updated_at=now, duration_seconds=seconds,
                              final=True, language=language, item_title=it.title, quality="position",
                              flags=["history", *flags])
        rule = self.src.library(LIBRARY.id, LIBRARY.name)
        decision = filters.evaluate(self.src, rule, language, seconds, self.cfg.min_session_minutes, True)
        if hold and (decision.keep or decision.review):
            decision = filters.Decision.hold(hold)
        return Polled(session_id, it.video_id, event, decision, f"{meta.title} / {it.title}")
