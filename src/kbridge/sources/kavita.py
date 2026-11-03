"""Kavita: /api/Stats/reading-history -> one log per reading session x chapter."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .. import filters, lang
from ..config import Config, EffectiveLibrary, SourceConfig
from ..db import DB
from ..models import ActivityEvent, SeriesMeta
from .base import Library, Polled

FINAL_AFTER = timedelta(minutes=30)    # Kavita closes a session after 30 idle minutes
OVERLAP = timedelta(days=1)            # sessions end by midnight; re-reading a day is cheap
SERIES_CACHE_TTL = timedelta(hours=12)
PUBLICATION_STATUS = {0: "Ongoing", 1: "Hiatus", 2: "Completed", 3: "Cancelled", 4: "Ended"}
LIBRARY_TYPES = {0: "manga", 1: "comic", 2: "book", 3: "images", 4: "lightnovel", 5: "comicvine"}


def _utc(value: str | None) -> datetime | None:
    """Kavita's *Utc fields carry no offset."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.rstrip("Z"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def chapter_note(label: str) -> str:
    """'Volume 3' -> 'Vol.3', 'Chapter 12' -> 'Ch.12', 'Volume 2 Chapter 11' -> 'Vol.2 Ch.11'; book chapter
    titles stay as they are."""
    label = (label or "").strip()
    m = re.fullmatch(r"(?i)vol(?:ume)?\.?\s*(\S+)\s+ch(?:apter)?\.?\s*(\S+)", label)
    if m:
        return f"Vol.{m.group(1)} Ch.{m.group(2)}"
    m = re.fullmatch(r"(?i)vol(?:ume)?\.?\s*(\S+)", label)
    if m:
        return f"Vol.{m.group(1)}"
    m = re.fullmatch(r"(?i)ch(?:apter)?\.?\s*(\S+)", label)
    if m:
        return f"Ch.{m.group(1)}"
    return label


def anilist_link(web_links: str | None) -> str | None:
    for link in re.split(r"[,\s]+", web_links or ""):
        if re.match(r"https?://anilist\.co/(manga|anime)/\d+", link):
            return link.rstrip("/")
    return None


def series_meta(series: dict[str, Any], metadata: dict[str, Any], library: EffectiveLibrary,
                library_id: str, library_name: str) -> SeriesMeta:
    name = (series.get("name") or "").strip()
    alts = [t for t in (series.get("originalName"), series.get("localizedName"), series.get("sortName"))
            if t and t.strip() and t.strip() != name]
    writers = ", ".join(w.get("name", "") for w in metadata.get("writers") or [] if w.get("name"))
    volumes = metadata.get("totalCount") or metadata.get("maxCount") or 0
    fields = {
        "Source (Anilist)": anilist_link(metadata.get("webLinks")) or "",
        "Library": f"Kavita · {library_name}",
        "English title": series.get("localizedName") if series.get("localizedName") not in (None, "", name) else "",
        "Author": writers,
        "Year": str(metadata["releaseYear"]) if metadata.get("releaseYear") else "",
        "Genres": ", ".join(g.get("title", "") for g in (metadata.get("genres") or [])[:6] if g.get("title")),
        "Status": PUBLICATION_STATUS.get(metadata.get("publicationStatus"), ""),
        "Volumes": str(volumes) if volumes else "",
    }
    return SeriesMeta(source="kavita", series_key=f"series:{series['id']}", title=name, library_id=library_id,
                      library_name=library_name, content_type=library.content_type,
                      activity_type=library.activity_type, description=(metadata.get("summary") or "").strip(),
                      language=lang.normalize(metadata.get("language")), alt_titles=alts, fields=fields,
                      cover_ref=str(series["id"]))


def normalize(session: dict[str, Any], meta: SeriesMeta | None, src: SourceConfig, library: EffectiveLibrary,
              cfg: Config, now: datetime) -> list[Polled]:
    """One Polled per chapter read in the session (each chapter carries its own duration)."""
    sid = f"kv:{session['sessionId']}"
    end = _utc(session.get("endTimeUtc")) or _utc(session.get("startTimeUtc"))
    final = end is not None and now - end >= FINAL_AFTER
    chapters = session.get("chapters") or [{
        "chapterId": "session", "label": "", "startTimeUtc": session.get("startTimeUtc"),
        "endTimeUtc": session.get("endTimeUtc"), "durationSeconds": session.get("durationSeconds")}]
    generated = bool(session.get("isGeneratedSession"))
    out = []
    for ch in chapters:
        item_key = str(ch.get("chapterId"))
        started = _utc(ch.get("startTimeUtc")) or _utc(session.get("startTimeUtc"))
        seconds = float(ch.get("durationSeconds") or 0)
        decision = filters.library_decision(src, library)
        if not decision.keep or meta is None:
            info = {"library": session.get("libraryName"), "title": session.get("seriesName"),
                    "note": chapter_note(ch.get("label", "")), "started_at": started.isoformat() if started else None,
                    "minutes": round(seconds / 60, 1), "quality": "measured",
                    "flags": ["generated"] if generated else []}
            out.append(Polled(sid, item_key, None,
                              decision if not decision.keep else filters.Decision.skip("series_missing"),
                              session.get("seriesName") or "", info))
            continue
        event = ActivityEvent(
            source="kavita", session_id=sid, item_key=item_key, series=meta,
            note=chapter_note(ch.get("label", "")) or meta.title, started_at=started,
            updated_at=_utc(ch.get("endTimeUtc")) or started, duration_seconds=seconds, final=final,
            language=meta.language, completed=bool(ch.get("completed")), item_title=ch.get("label", ""),
            flags=["generated"] if generated else [])
        decision = filters.evaluate(src, library, meta.language, seconds, cfg.min_session_minutes, final)
        if generated and decision.keep:
            decision = filters.policy(src.extra.get("generated_sessions", "ignore"), "generated")
        out.append(Polled(sid, item_key, event, decision, f"{meta.title} / {event.note}"))
    return out


class KavitaSource:
    name = "kavita"

    def __init__(self, cfg: Config, db: DB, client: httpx.AsyncClient | None = None):
        self.cfg, self.db = cfg, db
        self.src = cfg.source("kavita")
        self.client = client or httpx.AsyncClient(
            base_url=self.src.url, timeout=httpx.Timeout(60, connect=5),
            headers={"x-api-key": self.src.api_key})
        self._libraries: dict[str, Library] | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: Any = None) -> Any:
        r = await self.client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def list_libraries(self) -> list[Library]:
        libs = [Library(str(l["id"]), l["name"], LIBRARY_TYPES.get(l.get("type"), str(l.get("type"))))
                for l in await self._get("/api/Library/libraries")]
        self._libraries = {l.id: l for l in libs}
        return libs

    async def _series_meta(self, series_id: int, library: EffectiveLibrary, library_id: str,
                           library_name: str) -> SeriesMeta | None:
        key = f"kavita:series:{series_id}"
        cached = self.db.cache_get(key)
        if cached is None:
            try:
                series = await self._get(f"/api/Series/{series_id}")
                metadata = await self._get("/api/Series/metadata", {"seriesId": series_id})
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (400, 404):
                    return None
                raise
            cached = {"series": series, "metadata": metadata}
            self.db.cache_set(key, cached, datetime.now(timezone.utc) + SERIES_CACHE_TTL)
        return series_meta(cached["series"], cached["metadata"], library, library_id, library_name)

    async def sessions(self, start: datetime | None = None) -> list[dict[str, Any]]:
        if self._libraries is None:
            await self.list_libraries()
        assert self._libraries is not None
        # `Libraries` must be repeated; without it Kavita silently returns nothing.
        params: list[tuple[str, Any]] = [("Libraries", lib_id) for lib_id in self._libraries]
        params.append(("TimeZoneId", self.cfg.timezone))
        if start is not None:
            params.append(("StartDate", start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        return await self._get("/api/Stats/reading-history", params)

    async def _normalize_all(self, sessions: list[dict[str, Any]], now: datetime) -> list[Polled]:
        out: list[Polled] = []
        for s in sessions:
            lib_id = str(s.get("libraryId"))
            lib_name = (self._libraries or {}).get(lib_id, Library(lib_id, s.get("libraryName") or "?", "")).name
            rule = self.src.library(lib_id, lib_name)
            meta = await self._series_meta(s["seriesId"], rule, lib_id, lib_name) if rule.track else None
            out.extend(normalize(s, meta, self.src, rule, self.cfg, now))
        return out

    async def poll(self, since: datetime, now: datetime) -> list[Polled]:
        sessions = await self.sessions(since - OVERLAP)
        fresh = [s for s in sessions if (_utc(s.get("endTimeUtc")) or now) >= since - OVERLAP]
        return await self._normalize_all(fresh, now)

    async def history(self, now: datetime) -> list[Polled]:
        return await self._normalize_all(await self.sessions(), now)

    async def fetch_cover(self, ref: str) -> tuple[bytes, str, str] | None:
        r = await self.client.get("/api/Image/series-cover", params={"seriesId": ref})
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        ctype = r.headers.get("content-type", "image/webp")
        return r.content, "cover." + ctype.rsplit("/", 1)[-1], ctype
