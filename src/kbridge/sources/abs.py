"""Audiobookshelf: /api/me/listening-sessions -> one log per ABS session."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .. import filters, lang
from ..config import Config, EffectiveLibrary, SourceConfig
from ..db import DB
from ..models import ActivityEvent, SeriesMeta
from .base import Library, Polled

FINAL_AFTER = timedelta(minutes=30)
OVERLAP = timedelta(hours=2)
PAGE_SIZE = 100
ITEM_CACHE_TTL = timedelta(hours=6)


def _ms(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def clean_text(text: str | None) -> str:
    """Podcast/book descriptions arrive as HTML or with entities; kechimochi shows plain text."""
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _numeric(value: Any) -> bool:
    return value is not None and re.fullmatch(r"\d+(\.\d+)?", str(value).strip()) is not None


def episode_note(title: str, season: Any, episode: Any) -> str:
    title = (title or "").strip()
    if not _numeric(episode):
        return title
    label = f"#{season}-{episode}" if _numeric(season) else f"#{episode}"
    # Many feeds already put the number in the title ("427: …", "【65-4】…"); don't repeat it.
    number = str(episode).strip()
    if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", unicodedata.normalize("NFKC", title)[:20]):
        return title
    return f"{label} · {title}" if title else label


def _join(values: Any) -> str:
    if not values:
        return ""
    names = [v.get("name") if isinstance(v, dict) else str(v) for v in values]
    return ", ".join(n for n in names if n)


def series_meta(session: dict[str, Any], library: EffectiveLibrary, library_name: str,
                item: dict[str, Any] | None) -> tuple[SeriesMeta, str, str]:
    """Return (series metadata, item key, note) for a session."""
    m = session.get("mediaMetadata") or {}
    language = lang.normalize(m.get("language"))
    common = dict(source="abs", library_id=session.get("libraryId") or "", library_name=library_name,
                  content_type=library.content_type, activity_type=library.activity_type, language=language,
                  cover_ref=session["libraryItemId"])
    if session.get("mediaType") == "podcast":
        episodes = ((item or {}).get("media") or {}).get("episodes") or []
        ep = next((e for e in episodes if e.get("id") == session.get("episodeId")), {})
        fields = {
            "Library": f"ABS · {library_name}",
            "Author": m.get("author") or session.get("displayAuthor") or "",
            "Genres": ", ".join(g for g in m.get("genres") or [] if g != "Podcasts"),
            "Episodes": str(len(episodes)) if episodes else "",
            "Apple Podcasts": m.get("itunesPageUrl") or "",
        }
        meta = SeriesMeta(series_key=f"podcast:{session['libraryItemId']}",
                          title=(m.get("title") or session.get("displayAuthor") or "").strip(),
                          description=clean_text(m.get("description")), fields=fields, **common)
        note = episode_note(ep.get("title") or session.get("displayTitle") or "", ep.get("season"), ep.get("episode"))
        return meta, session.get("episodeId") or session["libraryItemId"], note

    series = [s for s in m.get("series") or [] if isinstance(s, dict) and s.get("name")]
    book_title = (m.get("title") or session.get("displayTitle") or "").strip()
    fields = {
        "Library": f"ABS · {library_name}",
        "Author": _join(m.get("authors")) or session.get("displayAuthor") or "",
        "Narrator": _join(m.get("narrators")),
        "Publisher": m.get("publisher") or "",
        "Year": str(m.get("publishedYear") or ""),
        "Genres": _join(m.get("genres")),
        "ASIN": m.get("asin") or "",
        "ISBN": m.get("isbn") or "",
    }
    nagare = [f"audiobookshelf|{session['libraryItemId']}"]
    if series:
        s = series[0]
        alt = [f"{s['name']} #{s['sequence']}"] if s.get("sequence") else []
        meta = SeriesMeta(series_key=f"series:{s.get('id') or s['name']}", title=s["name"].strip(),
                          alt_titles=alt, description=clean_text(m.get("description")),
                          fields={**fields, "ISBN": "", "ASIN": ""}, nagare_history_ids=nagare, **common)
        note = f"Vol.{s['sequence']} · {book_title}" if _numeric(s.get("sequence")) else book_title
    else:
        meta = SeriesMeta(series_key=f"book:{session['libraryItemId']}", title=book_title,
                          description=clean_text(m.get("description")), fields=fields,
                          nagare_history_ids=nagare, **common)
        chapter = _current_chapter(session)
        note = chapter or book_title
    return meta, session["libraryItemId"], note


def _current_chapter(session: dict[str, Any]) -> str:
    """For a standalone book, name the chapter the session started in (if the book has chapters)."""
    start = session.get("startTime") or 0
    for ch in session.get("chapters") or []:
        if ch.get("start", 0) <= start < ch.get("end", 0) and ch.get("title"):
            return ch["title"]
    return ""


def normalize(session: dict[str, Any], src: SourceConfig, library: EffectiveLibrary, library_name: str,
              item: dict[str, Any] | None, cfg: Config, now: datetime) -> Polled:
    started = _ms(session["startedAt"])
    updated = _ms(session.get("updatedAt") or session["startedAt"])
    seconds = float(session.get("timeListening") or 0)
    final = now - updated >= FINAL_AFTER
    language = (session.get("mediaMetadata") or {}).get("language")
    item_key = session.get("episodeId") or session["libraryItemId"]
    decision = filters.library_decision(src, library)
    if not decision.keep or not session.get("mediaMetadata"):
        info = {"library": library_name, "title": session.get("displayTitle") or "",
                "started_at": started.isoformat(), "minutes": round(seconds / 60, 1), "quality": "measured"}
        return Polled(session["id"], item_key, None,
                      decision if not decision.keep else filters.Decision.skip("no_metadata"),
                      session.get("displayTitle") or "", info)
    meta, item_key, note = series_meta(session, library, library_name, item)
    event = ActivityEvent(source="abs", session_id=session["id"], item_key=item_key, series=meta, note=note,
                          started_at=started, updated_at=updated, duration_seconds=seconds, final=final,
                          language=lang.normalize(language), item_title=session.get("displayTitle") or "",
                          nagare_key=f"audiobookshelf|{session['libraryItemId']}")
    decision = filters.evaluate(src, library, language, seconds, cfg.min_session_minutes, final)
    return Polled(session["id"], item_key, event, decision, f"{meta.title} / {note}")


class ABSSource:
    name = "abs"

    def __init__(self, cfg: Config, db: DB, client: httpx.AsyncClient | None = None):
        self.cfg, self.db = cfg, db
        self.src = cfg.source("abs")
        self.client = client or httpx.AsyncClient(
            base_url=self.src.url, timeout=httpx.Timeout(30, connect=5),
            headers={"Authorization": f"Bearer {self.src.api_key}"})
        self._libraries: dict[str, Library] | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self.client.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def list_libraries(self) -> list[Library]:
        data = await self._get("/api/libraries")
        libs = [Library(l["id"], l["name"], l.get("mediaType", "")) for l in data["libraries"]]
        self._libraries = {l.id: l for l in libs}
        return libs

    async def _item(self, library_item_id: str) -> dict[str, Any] | None:
        key = f"abs:item:{library_item_id}"
        cached = self.db.cache_get(key)
        if cached is not None:
            return cached
        try:
            item = await self._get(f"/api/items/{library_item_id}", expanded=1)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        slim = {"media": {"episodes": [
            {k: e.get(k) for k in ("id", "title", "season", "episode", "publishedAt")}
            for e in (item.get("media") or {}).get("episodes") or []]}}
        self.db.cache_set(key, slim, datetime.now(timezone.utc) + ITEM_CACHE_TTL)
        return slim

    async def sessions_since(self, since: datetime) -> list[dict[str, Any]]:
        """Newest-first pages; stop once a whole page was last updated before `since`."""
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            data = await self._get("/api/me/listening-sessions", itemsPerPage=PAGE_SIZE, page=page)
            sessions = data.get("sessions") or []
            fresh = [s for s in sessions if _ms(s.get("updatedAt") or s["startedAt"]) >= since]
            out.extend(fresh)
            page += 1
            if not fresh or page >= int(data.get("numPages") or 0):
                return out

    async def poll(self, since: datetime, now: datetime) -> list[Polled]:
        if self._libraries is None:
            await self.list_libraries()
        assert self._libraries is not None
        results = []
        for s in await self.sessions_since(since - OVERLAP):
            lib_id = s.get("libraryId") or ""
            lib = self._libraries.get(lib_id)
            lib_name = lib.name if lib else "(deleted library)"
            rule = self.src.library(lib_id, lib_name)
            item = None
            if s.get("mediaType") == "podcast" and rule.track:
                item = await self._item(s["libraryItemId"])
            results.append(normalize(s, self.src, rule, lib_name, item, self.cfg, now))
        return results

    async def history(self, now: datetime) -> list[Polled]:
        """Every listening session ABS has, for the backfill."""
        return await self.poll(datetime(1970, 1, 1, tzinfo=timezone.utc) + OVERLAP, now)

    async def fetch_cover(self, ref: str) -> tuple[bytes, str, str] | None:
        r = await self.client.get(f"/api/items/{ref}/cover", params={"format": "jpeg", "width": 600})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content, "cover.jpg", r.headers.get("content-type", "image/jpeg")
