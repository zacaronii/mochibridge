"""YouTube: browser-extension sessions, share-sheet logs and (separately) the watch-history reader.

YouTube has no API for watch history, so viewings arrive three ways (priority when they overlap):
extension (measured) > share sheet (your minutes) > history (estimated from the watched bar). This module holds the
shared metadata/entry logic and the session-based inputs; `youtube_history.py` adds the history reader."""

from __future__ import annotations

import html
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .. import filters
from ..config import Config
from ..db import DB, iso, parse_iso, utcnow
from ..models import ActivityEvent, SeriesMeta
from .base import Library, Polled

log = logging.getLogger(__name__)
LIBRARY = Library("youtube", "YouTube", "youtube")
SESSION_FINAL_AFTER = timedelta(minutes=10)
META_TTL = timedelta(days=30)
_KANA = re.compile(r"[぀-ヿ]")          # hiragana + katakana: Japanese, not just CJK
_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")
_PLAYER = re.compile(r"var ytInitialPlayerResponse\s*=\s*(\{.+?\});\s*(?:var|</script>)", re.S)
HEADERS = {"Accept-Language": "ja,en;q=0.5",
           "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def video_id_from(url_or_id: str) -> str | None:
    s = (url_or_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = _VIDEO_ID.search(s)
    return m.group(1) if m else None


def language_of(*texts: str | None) -> str | None:
    """Kana in the title or channel name -> Japanese. Otherwise unknown (the source sends those to review)."""
    return "ja" if any(t and _KANA.search(t) for t in texts) else None


def parse_watch_page(html: str) -> dict[str, Any]:
    """The public watch page embeds the player response: title, channel, length. Unofficial; fails soft."""
    m = _PLAYER.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    v = data.get("videoDetails") or {}
    mf = (data.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    return {k: val for k, val in {
        "video_id": v.get("videoId"), "title": v.get("title"), "channel_id": v.get("channelId"),
        "channel": v.get("author"), "length": float(v["lengthSeconds"]) if v.get("lengthSeconds") else None,
        "channel_url": (mf.get("ownerProfileUrl") or "").replace("http://", "https://") or None,
        "is_short": bool(mf.get("isShortsEligible")) or None,
    }.items() if val is not None}


def channel_meta(info: dict[str, Any], channel: dict[str, Any] | None = None) -> SeriesMeta:
    """One kechimochi entry per channel, keeping the `youtube: {channel_id, channel_url}` shape other tools use."""
    cid = info["channel_id"]
    url = info.get("channel_url") or f"https://www.youtube.com/channel/{cid}"
    name = info.get("channel") or (channel or {}).get("title") or cid
    return SeriesMeta(
        source="youtube", series_key=f"channel:{cid}", title=name, library_id=LIBRARY.id, library_name=LIBRARY.name,
        content_type="Youtube Video", activity_type="Watching", description=(channel or {}).get("description", ""),
        language=language_of(name), fields={"youtube": {"channel_id": cid, "channel_url": url}, "Library": "YouTube"},
        cover_ref=(channel or {}).get("avatar"), match_ids={"youtube.channel_id": cid})


class YouTubeMeta:
    """Video/channel metadata from the public pages (no account, no key), cached in the DB."""

    def __init__(self, db: DB, client: httpx.AsyncClient | None = None):
        self.db = db
        self.client = client or httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True)

    async def video(self, video_id: str, hint: dict[str, Any] | None = None) -> dict[str, Any]:
        key = f"youtube:video:{video_id}"
        info = dict(self.db.cache_get(key) or {})
        hint = {k: v for k, v in (hint or {}).items() if v not in (None, "")}
        if not info.get("channel_id") or not info.get("length"):
            try:
                r = await self.client.get("https://www.youtube.com/watch", params={"v": video_id})
                info.update(parse_watch_page(r.text))
            except httpx.HTTPError as e:
                log.warning("watch page for %s failed: %s", video_id, e)
            if not info.get("title"):
                try:
                    r = await self.client.get("https://www.youtube.com/oembed",
                                              params={"url": f"https://youtu.be/{video_id}", "format": "json"})
                    if r.status_code == 200:
                        o = r.json()
                        info.setdefault("title", o.get("title"))
                        info.setdefault("channel", o.get("author_name"))
                        info.setdefault("channel_url", o.get("author_url"))
                except (httpx.HTTPError, ValueError):
                    pass
        info = {**info, **hint, "video_id": video_id}
        if info.get("channel_id"):
            self.db.cache_set(key, info, utcnow() + META_TTL)
        return info

    async def channel(self, channel_id: str) -> dict[str, Any]:
        key = f"youtube:channel:{channel_id}"
        cached = self.db.cache_get(key)
        if cached is not None:
            return cached
        info: dict[str, Any] = {}
        try:
            r = await self.client.get(f"https://www.youtube.com/channel/{channel_id}")
            for prop in ("title", "image", "description"):
                m = re.search(rf'<meta property="og:{prop}" content="([^"]*)"', r.text)
                if m:
                    info["avatar" if prop == "image" else prop] = html.unescape(m.group(1))
        except httpx.HTTPError as e:
            log.warning("channel page %s failed: %s", channel_id, e)
        self.db.cache_set(key, info, utcnow() + (META_TTL if info else timedelta(hours=1)))
        return info


class YouTubeSource:
    name = "youtube"

    def __init__(self, cfg: Config, db: DB, meta: YouTubeMeta | None = None):
        self.cfg, self.db = cfg, db
        self.src = cfg.source("youtube")
        self.meta = meta or YouTubeMeta(db)
        self.history_reader = None   # youtube_history.HistoryReader (set up below unless turned off)
        if self.src.extra.get("history", True):
            from pathlib import Path
            from .youtube_history import HistoryReader
            cookies = Path(self.src.extra.get("cookies_path") or Path(cfg.db_path).parent / "youtube-cookies.txt")
            self.history_reader = HistoryReader(cfg, db, self.meta, cookies)

    async def aclose(self) -> None:
        await self.meta.client.aclose()

    async def list_libraries(self) -> list[Library]:
        return [LIBRARY]

    # ------------------------------------------------------------- inputs (API)
    def record_progress(self, session_id: str, video_id: str, seconds: float, position: float | None,
                        payload: dict[str, Any], now: datetime | None = None) -> None:
        """Extension heartbeat: seconds only ever grow for a session."""
        now = now or utcnow()
        self.db.execute(
            "INSERT INTO yt_sessions(session_id, video_id, kind, seconds, position, started_at, updated_at, payload_json)"
            " VALUES (?,?, 'extension', ?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
            " seconds=max(yt_sessions.seconds, excluded.seconds), position=excluded.position,"
            " updated_at=excluded.updated_at, payload_json=excluded.payload_json",
            (session_id, video_id, float(seconds), position, iso(now - timedelta(seconds=float(seconds))), iso(now),
             json.dumps(payload, ensure_ascii=False)))

    async def record_share(self, url: str, minutes: float | None, now: datetime | None = None) -> dict[str, Any]:
        """Share-sheet / Quick log. Minutes default to the video's length."""
        now = now or utcnow()
        video_id = video_id_from(url)
        if not video_id:
            raise ValueError("that doesn't look like a YouTube link")
        info = await self.meta.video(video_id)
        if not info.get("channel_id"):
            raise ValueError("couldn't read that video's channel from YouTube")
        if minutes is None:
            if not info.get("length"):
                raise ValueError("video length unknown; enter the minutes you watched")
            seconds = float(info["length"])
        else:
            seconds = float(minutes) * 60
        session_id = f"share-{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO yt_sessions(session_id, video_id, kind, seconds, position, started_at, updated_at, payload_json)"
            " VALUES (?,?, 'share', ?, NULL, ?,?,?)",
            (session_id, video_id, seconds, iso(now - timedelta(seconds=seconds)), iso(now),
             json.dumps({"minutes_given": minutes}, ensure_ascii=False)))
        return {"session_id": session_id, "video_id": video_id, "title": info.get("title"),
                "channel": info.get("channel"), "minutes": round(seconds / 60)}

    # ------------------------------------------------------------- events
    def _retract_history(self, video_id: str, started: datetime, kind: str) -> None:
        """Measured (extension) or your own minutes (share) win: drop the history estimate for the same viewing,
        whichever arrived first."""
        h = self.db.one("SELECT cur_session, cur_started FROM yt_history WHERE video_id=? AND cur_session IS NOT NULL",
                        (video_id,))
        if h is None or (parse_iso(h["cur_started"]) or started) < started - timedelta(minutes=30):
            return
        sid = f"yt:{h['cur_session']}"
        hit = self.db.execute("UPDATE logs SET retracted=1 WHERE source='youtube' AND session_id=? AND retracted=0",
                              (sid,)).rowcount
        hit += self.db.execute("DELETE FROM skips WHERE source='youtube' AND session_id=?", (sid,)).rowcount
        hit += self.db.execute("DELETE FROM reviews WHERE source='youtube' AND session_id=? AND status='pending'",
                               (sid,)).rowcount
        if hit:
            self.db.event("info", "youtube", f"history estimate for {video_id} replaced by the {kind} session")

    async def _event(self, row, now: datetime) -> Polled:
        self._retract_history(row["video_id"], parse_iso(row["started_at"]), row["kind"])
        payload = json.loads(row["payload_json"] or "{}")
        hint = {k: payload.get(k) for k in ("title", "channel_id", "channel", "length", "channel_url")}
        info = await self.meta.video(row["video_id"], hint)
        sid = f"yt:{row['session_id']}"
        if not info.get("channel_id"):
            return Polled(sid, row["video_id"], None, filters.Decision.skip("metadata_missing"), row["video_id"])
        meta = channel_meta(info, await self.meta.channel(info["channel_id"]))
        started, updated = parse_iso(row["started_at"]), parse_iso(row["updated_at"])
        final = row["kind"] == "share" or now - updated >= SESSION_FINAL_AFTER
        language = language_of(info.get("title"), info.get("channel"))
        title = (info.get("title") or row["video_id"]).strip()
        event = ActivityEvent(source="youtube", session_id=sid, item_key=row["video_id"], series=meta,
                              note=f"{title} · https://youtu.be/{row['video_id']}", started_at=started,
                              updated_at=updated, duration_seconds=float(row["seconds"]), final=final,
                              language=language, item_title=title,
                              quality="measured" if row["kind"] == "extension" else "manual",
                              flags=[row["kind"]])
        rule = self.src.library(LIBRARY.id, LIBRARY.name)
        decision = filters.evaluate(self.src, rule, language, float(row["seconds"]), self.cfg.min_session_minutes, final)
        if row["kind"] == "share" and decision.reason in ("too_short",):
            decision = filters.Decision.ok() if row["seconds"] >= 60 else decision   # you said you watched it
        return Polled(sid, row["video_id"], event, decision, f"{meta.title} / {title}")

    async def poll(self, since: datetime, now: datetime) -> list[Polled]:
        """Sessions that changed since the last poll, plus ones that just became final."""
        rows = self.db.all("SELECT * FROM yt_sessions WHERE updated_at >= ? OR (kind='extension' AND emitted_at IS NULL)"
                           " OR (kind='extension' AND updated_at >= ?)",
                           (iso(since - timedelta(seconds=5)), iso(now - SESSION_FINAL_AFTER - timedelta(minutes=2))))
        out = []
        for row in rows:
            out.append(await self._event(row, now))
            self.db.execute("UPDATE yt_sessions SET emitted_at=? WHERE session_id=?", (iso(now), row["session_id"]))
        if self.history_reader is not None:
            out += await self.history_reader.poll(since, now)
        return out

    async def history(self, now: datetime) -> list[Polled]:
        """Backfill hook used by `kbridge backfill`; YouTube has no backfill yet."""
        return []

    async def fetch_cover(self, ref: str) -> tuple[bytes, str, str] | None:
        if not ref or not ref.startswith("https://"):
            return None
        r = await self.meta.client.get(ref)
        if r.status_code != 200:
            return None
        ctype = r.headers.get("content-type", "image/jpeg")
        return r.content, "avatar." + ctype.rsplit("/", 1)[-1], ctype
