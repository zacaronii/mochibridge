"""Jellyfin: metadata client + history from Nagare's records.

Live playback comes from /Sessions audio sampling (jfplays) with Playback Reporting as a backstop. Before
that, Nagare's per-episode watches are the only real Jellyfin history; Jellyfin's own "played" flags are
offered as excluded-by-default estimates."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .. import anilist, filters, idmap, lang
from ..config import Config, EffectiveLibrary, SourceConfig
from ..db import DB
from ..models import ActivityEvent, SeriesMeta
from ..resolver import normalize_title
from .base import Library, Polled
from .jfplays import Sample, counted, group_plays, watched_by_language

log = logging.getLogger(__name__)
ITEM_TTL = timedelta(hours=12)
LIBMAP_TTL = timedelta(hours=24)
SAMPLE_RETENTION = timedelta(days=30)
PLUGIN_MATCH_SLACK = timedelta(minutes=2)
PLUGIN_SETTLE = timedelta(minutes=10)
ITEM_FIELDS = "Overview,Genres,Studios,ProviderIds,MediaStreams,OriginalTitle,SortName,ProductionYear,UserData"


def _ticks_to_seconds(ticks: Any) -> float:
    return (ticks or 0) / 10_000_000


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    if "." in v:   # Jellyfin/.NET sends 7 fractional digits; Python takes 6
        head, rest = v.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        v = f"{head}.{frac[:6]}{tz}"
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def audio_language(item: dict[str, Any], prefer: list[str] | None = None) -> str | None:
    """Language to judge an item by when we don't know which track was played.

    Default: the default audio track (or the only/first one), as the spec says. With `prefer` (the
    `audio_fallback: any` setting), an item that has a track in one of the preferred languages counts as that
    language — useful for dual-audio anime whose default track is the dub."""
    tracks = [s for s in item.get("MediaStreams") or [] if s.get("Type") == "Audio"]
    if not tracks:
        return None
    if prefer:
        for t in tracks:
            code = lang.normalize(t.get("Language"))
            if code in prefer:
                return code
    track = next((t for t in tracks if t.get("IsDefault")), tracks[0])
    return lang.normalize(track.get("Language"))


def episode_note(item: dict[str, Any]) -> str:
    name = (item.get("Name") or "").strip()
    if item.get("Type") != "Episode":
        return name
    s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
    if e is None:
        return name
    label = f"S{int(s or 0):02d}E{int(e):02d}"
    return f"{label} · {name}" if name else label


def provider_links(db: DB, kind: str, providers: dict[str, str], season: int | None = None) -> dict[str, str]:
    """Readable links plus cross-database IDs via anibridge's map."""
    p = {k.lower(): v for k, v in (providers or {}).items()}
    ids: dict[str, str] = {}
    if p.get("anidb"):
        ids = idmap.lookup(db, "anidb", p["anidb"])
    elif kind == "Movie":
        for prov, key in (("tmdb_movie", "tmdb"), ("imdb_movie", "imdb"), ("tvdb_movie", "tvdb")):
            if p.get(key):
                ids = idmap.lookup(db, prov, p[key])
                if ids:
                    break
    else:
        scope = f"s{season or 1}"
        for prov, key in (("tvdb_show", "tvdb"), ("tmdb_show", "tmdb")):
            if p.get(key):
                ids = idmap.lookup(db, prov, p[key], scope=scope)
                if ids:
                    break
    links = {}
    if ids.get("anilist"):
        links["Source (Anilist)"] = f"https://anilist.co/anime/{ids['anilist'].split(':')[0]}"
    if p.get("imdb"):
        links["Source (IMDB)"] = f"https://www.imdb.com/title/{p['imdb']}"
    if p.get("anidb"):
        links["AniDB"] = f"https://anidb.net/anime/{p['anidb']}"
    if ids.get("mal"):
        links["MAL"] = f"https://myanimelist.net/anime/{ids['mal'].split(':')[0]}"
    links["_anilist_id"] = ids.get("anilist", "").split(":")[0]
    return links


def series_meta(db: DB, series: dict[str, Any], library: EffectiveLibrary, library_id: str, library_name: str,
                alt_titles: list[str], links: dict[str, str]) -> SeriesMeta:
    kind = series.get("Type")
    studios: list[str] = []
    seen: set[str] = set()
    for st in series.get("Studios") or []:
        name = (st.get("Name") or "").strip()
        key = "".join(ch for ch in name.lower() if ch.isalnum())   # "Studio Name" == "studioname"
        if name and key not in seen:
            seen.add(key)
            studios.append(name)
    fields = {
        **{k: v for k, v in links.items() if not k.startswith("_")},
        "Library": f"Jellyfin · {library_name}",
        "Year": str(series.get("ProductionYear") or ""),
        "Studio": ", ".join(studios[:3]),
        "Genres": ", ".join((series.get("Genres") or [])[:6]),
        "Status": series.get("Status") or "",
    }
    name = (series.get("Name") or "").strip()
    alts = [t for t in [series.get("OriginalTitle"), *alt_titles] if t and t.strip() and t.strip() != name]
    key = f"series:{series['Id']}" if kind == "Series" else f"movie:{series['Id']}"
    return SeriesMeta(source="jellyfin", series_key=key, title=name, library_id=library_id,
                      library_name=library_name, content_type=library.content_type,
                      activity_type=library.activity_type, description=(series.get("Overview") or "").strip(),
                      alt_titles=alts, fields=fields, cover_ref=series["Id"])


def read_nagare(path: str) -> list[dict[str, Any]]:
    """Nagare's Jellyfin watches (one row per watch, rewatches included). Opened read-only."""
    uri = Path(path).resolve().as_uri()
    try:
        conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM media_watch_history LIMIT 1")
    except sqlite3.Error:
        conn = sqlite3.connect(f"{uri}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT history_id, media_history_id, watch_number, item_id, title, series_name, duration_ms,"
            " last_position_ms, last_seen, audio_languages_json FROM media_watch_history"
            " WHERE server_kind='jellyfin' ORDER BY last_seen")]
    finally:
        conn.close()


class JellyfinSource:
    name = "jellyfin"

    def __init__(self, cfg: Config, db: DB, client: httpx.AsyncClient | None = None,
                 anilist_client: httpx.AsyncClient | None = None):
        self.cfg, self.db = cfg, db
        self.src: SourceConfig = cfg.source("jellyfin")
        self.client = client or httpx.AsyncClient(
            base_url=self.src.url, timeout=httpx.Timeout(30, connect=5),
            headers={"Authorization": f'MediaBrowser Token="{self.src.api_key}"'})
        self.anilist_client = anilist_client
        self._user_id: str | None = None
        self._libraries: dict[str, Library] | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self.client.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def user_id(self) -> str:
        if self._user_id is None:
            users = await self._get("/Users")
            me = [u for u in users if u["Name"].lower() == self.src.user.lower()]
            if len(me) != 1:
                raise RuntimeError(f"Jellyfin user {self.src.user!r} not found")
            self._user_id = me[0]["Id"]
        return self._user_id

    async def list_libraries(self) -> list[Library]:
        libs = [Library(l["ItemId"], l["Name"], l.get("CollectionType") or "")
                for l in await self._get("/Library/VirtualFolders")]
        self._libraries = {l.id: l for l in libs}
        return libs

    async def _scan(self) -> dict[str, Any]:
        """List each library's top-level series/movies once a day: series -> library (Ancestors don't show the
        library under Shokofin's VFS) and a title index used to recover watches whose item IDs changed."""
        cached = self.db.cache_get("jellyfin:scan")
        if cached is not None:
            return cached
        if self._libraries is None:
            await self.list_libraries()
        uid = await self.user_id()
        libmap: dict[str, str] = {}
        titles: dict[str, list[str]] = {}
        anidb: dict[str, str] = {}
        for lib in (self._libraries or {}).values():
            data = await self._get(f"/Users/{uid}/Items", ParentId=lib.id, Recursive="true",
                                   IncludeItemTypes="Series,Movie", Fields="ProviderIds,OriginalTitle,SortName")
            for it in data.get("Items") or []:
                libmap.setdefault(it["Id"], lib.id)
                titles[it["Id"]] = [t for t in (it.get("Name"), it.get("OriginalTitle")) if t]
                prov = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items()}
                if prov.get("anidb"):
                    anilist_id = idmap.anilist_id(self.db, "anidb", prov["anidb"])
                    if anilist_id:
                        anidb[it["Id"]] = anilist_id
        if self.cfg.anilist_titles and anidb:
            got = await anilist.titles(self.db, list(anidb.values()), self.anilist_client)
            for sid, aid in anidb.items():
                titles[sid] += got.get(aid, [])
        index: dict[str, list[str]] = {}
        for sid, names in titles.items():
            for n in names:
                key = normalize_title(n)
                if key and sid not in index.setdefault(key, []):
                    index[key].append(sid)
        scan = {"libmap": libmap, "index": index}
        self.db.cache_set("jellyfin:scan", scan, datetime.now(timezone.utc) + LIBMAP_TTL)
        return scan

    async def library_map(self) -> dict[str, str]:
        return (await self._scan())["libmap"]

    async def episodes(self, series_id: str) -> list[dict[str, Any]]:
        key = f"jellyfin:episodes:{series_id}"
        cached = self.db.cache_get(key)
        if cached is not None:
            return cached
        data = await self._get(f"/Shows/{series_id}/Episodes", UserId=await self.user_id(), Fields=ITEM_FIELDS)
        eps = data.get("Items") or []
        self.db.cache_set(key, eps, datetime.now(timezone.utc) + ITEM_TTL)
        return eps

    async def recover(self, title: str | None, series_name: str | None,
                      audio_languages: list[str] | None = None) -> dict[str, Any] | None:
        """Find today's item for a watch whose Jellyfin item no longer exists (e.g. after a library is re-imported
        through Shokofin): series by any known title, then the episode by season/episode number. If the series exists
        but the episode doesn't (a file the library never linked), return a stand-in episode built from Nagare's
        title and audio list, marked `_episode_missing`."""
        if not series_name:
            return None
        sids = (await self._scan())["index"].get(normalize_title(series_name), [])
        if len(sids) != 1:
            return None
        m = re.search(r"\bS(\d+)E(\d+)\b", title or "")
        if not m:
            it = await self.item(sids[0])
            return it if it and it.get("Type") == "Movie" else None
        season, number = int(m.group(1)), int(m.group(2))
        for ep in await self.episodes(sids[0]):
            if ep.get("ParentIndexNumber") == season and ep.get("IndexNumber") == number:
                self.db.cache_set(f"jellyfin:item:{ep['Id']}", ep, datetime.now(timezone.utc) + ITEM_TTL)
                return ep
        name = (title or "")[m.end():].lstrip(" -–·:").strip()
        return {"Id": f"missing:{sids[0]}:{season}:{number}", "Type": "Episode", "SeriesId": sids[0],
                "ParentIndexNumber": season, "IndexNumber": number, "Name": name, "_episode_missing": True,
                "MediaStreams": [{"Type": "Audio", "Language": l} for l in audio_languages or []]}

    async def item(self, item_id: str) -> dict[str, Any] | None:
        key = f"jellyfin:item:{item_id}"
        cached = self.db.cache_get(key)
        if cached is not None:
            return cached or None
        uid = await self.user_id()
        try:
            it = await self._get(f"/Users/{uid}/Items/{item_id}", Fields=ITEM_FIELDS)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 404):
                raise
            it = {}
        self.db.cache_set(key, it, datetime.now(timezone.utc) + ITEM_TTL)
        return it or None

    async def playback_reporting_start(self) -> datetime | None:
        """First Playback Reporting row for this user; Nagare history is only used before it."""
        uid = (await self.user_id()).replace("-", "")
        try:
            r = await self.client.post("/user_usage_stats/submit_custom_query", json={
                "CustomQueryString": f"SELECT MIN(DateCreated) FROM PlaybackActivity WHERE UserId='{uid}'",
                "ReplaceUserId": False})
            r.raise_for_status()
            value = (r.json().get("results") or [[None]])[0][0]
        except (httpx.HTTPError, ValueError, IndexError):
            return None
        return _dt(value.replace(" ", "T")) if value else None

    async def _series_for(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if item.get("Type") == "Episode" and item.get("SeriesId"):
            return await self.item(item["SeriesId"])
        return item

    async def _library_rule(self, series_id: str) -> tuple[str, str, EffectiveLibrary]:
        lib_id = (await self.library_map()).get(series_id, "")
        lib = (self._libraries or {}).get(lib_id)
        name = lib.name if lib else "(unknown library)"
        return lib_id, name, self.src.library(lib_id, name)

    async def _build(self, rows: list[tuple[dict[str, Any], dict[str, Any] | None]], now: datetime,
                     nagare_by_series: dict[str, list[str]]) -> list[Polled]:
        """rows: (watch description, Jellyfin item or None)."""
        series_items: dict[str, dict[str, Any]] = {}
        links: dict[str, dict[str, str]] = {}
        for w, it in rows:
            if it is None:
                continue
            s = await self._series_for(it)
            if s:
                series_items[s["Id"]] = s
                links[s["Id"]] = provider_links(self.db, s.get("Type", ""), s.get("ProviderIds") or {},
                                                it.get("ParentIndexNumber"))
        alt: dict[str, list[str]] = {}
        if self.cfg.anilist_titles:
            got = await anilist.titles(self.db, [l["_anilist_id"] for l in links.values() if l["_anilist_id"]],
                                       self.anilist_client)
            alt = {sid: got.get(l["_anilist_id"], []) for sid, l in links.items()}
        metas: dict[str, SeriesMeta] = {}
        out: list[Polled] = []
        for w, it in rows:
            sid, ik = w["session_id"], w["item_id"]
            started = w["started_at"]
            seconds = w["seconds"]
            info = {"library": "", "title": w.get("series_name") or w.get("title"), "note": w.get("title"),
                    "started_at": started.isoformat(), "minutes": round(seconds / 60, 1),
                    "quality": w["quality"], "flags": list(w["flags"]), "nagare_key": w.get("nagare_key")}
            series = await self._series_for(it) if it else None
            if not it or not series:
                out.append(Polled(sid, ik, None, filters.Decision.skip("item_missing"), info["title"] or "",
                                  {**info, "flags": info["flags"] + ["item_missing"]}))
                continue
            lib_id, lib_name, rule = await self._library_rule(series["Id"])
            if series["Id"] not in metas:
                metas[series["Id"]] = series_meta(self.db, series, rule, lib_id, lib_name,
                                                  alt.get(series["Id"], []), links.get(series["Id"], {}))
            meta = metas[series["Id"]]
            meta.nagare_history_ids = nagare_by_series.get(series["Id"], [])
            if "by_lang" in w:     # live play: only time with an allowed audio track counts
                seconds, language = counted(w["by_lang"], rule.languages, rule.unknown_language)
            else:
                prefer = rule.languages if self.src.extra.get("audio_fallback", "default") == "any" else None
                language = audio_language(it, prefer)
            final = w.get("final", True)
            event = ActivityEvent(source="jellyfin", session_id=sid, item_key=ik, series=meta,
                                  note=episode_note(it), started_at=started,
                                  updated_at=w.get("updated_at", started),
                                  duration_seconds=seconds, final=final, language=language,
                                  item_title=it.get("Name") or "", quality=w["quality"], flags=list(w["flags"]),
                                  nagare_key=w.get("nagare_key"))
            if "by_lang" in w and w["by_lang"] and not seconds:
                decision = filters.Decision.skip(f"language:{language}" if language else "language:unknown")
            else:
                decision = filters.evaluate(self.src, rule, language, seconds, self.cfg.min_session_minutes, final)
            if decision.keep and w["quality"] == "estimate":
                decision = filters.policy(self.src.extra.get("played_flags", "ignore"), "played_flag_only")
            out.append(Polled(sid, ik, event, decision, f"{meta.title} / {event.note}"))
        return out

    async def history(self, now: datetime, nagare_db: str | None = None) -> list[Polled]:
        nagare_db = nagare_db or self.src.extra.get("nagare_db")
        watches = []
        if nagare_db and Path(nagare_db).exists():
            watches = read_nagare(nagare_db)
        elif nagare_db:
            log.warning("Nagare DB %s not found; Jellyfin history will only have estimates", nagare_db)
        # Nagare covers Jellyfin until an explicit cut-over (set once live tracking is proven).
        # Not the plugin's first row: Playback Reporting can under-record or miss plays entirely, so it can't be
        # trusted to take over automatically.
        cutoff = self.nagare_until()
        rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        nagare_items: set[str] = set()
        nagare_by_series: dict[str, list[str]] = {}
        for w in watches:
            seen = _dt(w["last_seen"])
            nagare_items.add(w["item_id"])
            it = await self.item(w["item_id"])
            recovered = False
            if it is None:
                try:
                    audio = json.loads(w.get("audio_languages_json") or "[]")
                except ValueError:
                    audio = []
                it = await self.recover(w["title"], w["series_name"], audio)
                recovered = it is not None
                if it:
                    nagare_items.add(it["Id"])
            if it:
                s = await self._series_for(it)
                if s:
                    nagare_by_series.setdefault(s["Id"], []).append(f"jellyfin|{w['item_id']}")
            if cutoff and seen and seen >= cutoff:
                continue   # the live Jellyfin source covers this period
            duration = w["duration_ms"] or 0
            pos = min(w["last_position_ms"] or 0, duration) if duration else (w["last_position_ms"] or 0)
            rows.append(({"session_id": f"nagare:{w['history_id']}", "item_id": w["item_id"], "started_at": seen,
                          "seconds": pos / 1000, "quality": "position",
                          "flags": (["rewatch"] if w["watch_number"] else []) + (["recovered"] if recovered else [])
                                   + (["episode_missing"] if it and it.get("_episode_missing") else []),
                          "title": w["title"],
                          "series_name": w["series_name"], "nagare_key": f"jellyfin|{w['item_id']}"}, it))
        # Jellyfin "played" flags for items Nagare never saw: runtime-based estimates, excluded by default.
        uid = await self.user_id()
        played = await self._get(f"/Users/{uid}/Items", Recursive="true", IncludeItemTypes="Episode,Movie",
                                 Filters="IsPlayed", Fields=ITEM_FIELDS + ",SeriesId,RunTimeTicks")
        for it in played.get("Items") or []:
            last = _dt((it.get("UserData") or {}).get("LastPlayedDate"))
            if it["Id"] in nagare_items or last is None or (cutoff and last >= cutoff):
                continue
            self.db.cache_set(f"jellyfin:item:{it['Id']}", it, datetime.now(timezone.utc) + ITEM_TTL)
            rows.append(({"session_id": f"jfplayed:{it['Id']}", "item_id": it["Id"], "started_at": last,
                          "seconds": _ticks_to_seconds(it.get("RunTimeTicks")), "quality": "estimate",
                          "flags": ["estimate"], "title": it.get("Name"), "series_name": it.get("SeriesName")}, it))
        for sid in nagare_by_series:
            nagare_by_series[sid] = sorted(set(nagare_by_series[sid]))
        return await self._build(rows, now, nagare_by_series)

    def nagare_until(self) -> datetime | None:
        """When live tracking took over from Nagare: set by `kbridge jellyfin-golive` (DB), else config."""
        value = self.db.get_cursor("jellyfin", "nagare_until") or self.src.extra.get("nagare_until")
        return _dt(str(value)) if value else None

    @property
    def play_gap(self) -> timedelta:
        return timedelta(minutes=float(self.src.extra.get("play_gap_minutes", 5)))

    # ------------------------------------------------------------- live sampling
    async def now_playing(self) -> list[dict[str, Any]]:
        uid = await self.user_id()
        return [s for s in await self._get("/Sessions") if s.get("UserId") == uid and s.get("NowPlayingItem")]

    async def _stream_language(self, item_id: str, index: int | None) -> str | None:
        """Language of the selected audio track. Clients that don't report `AudioStreamIndex` get the
        same rule as Nagare history: `audio_fallback`."""
        it = await self.item(item_id)
        if index is None:
            prefer = self.src.languages if self.src.extra.get("audio_fallback", "default") == "any" else None
            return audio_language(it or {}, prefer)
        for s in (it or {}).get("MediaStreams") or []:
            if s.get("Type") == "Audio" and s.get("Index") == index:
                return lang.normalize(s.get("Language"))
        return None

    async def sample(self, now: datetime) -> int:
        """Record one /Sessions sample per playing session. Returns how many sessions were playing."""
        playing = await self.now_playing()
        for s in playing:
            st = s.get("PlayState") or {}
            item_id = s["NowPlayingItem"]["Id"]
            audio = st.get("AudioStreamIndex")
            self.db.execute(
                "INSERT INTO jf_samples(session_key, item_id, ts, position_ticks, is_paused, audio_index, audio_lang)"
                " VALUES (?,?,?,?,?,?,?)",
                (s["Id"], item_id, now.isoformat(), st.get("PositionTicks"), int(bool(st.get("IsPaused"))), audio,
                 await self._stream_language(item_id, audio)))
        self.db.execute("DELETE FROM jf_samples WHERE ts < ?", ((now - SAMPLE_RETENTION).isoformat(),))
        return len(playing)

    def plays(self, since: datetime, lookback: timedelta = timedelta(hours=12)) -> list:
        """Plays with a sample at or after `since - play_gap`, rebuilt from *all* their samples (a play that
        started hours ago must keep its start time and full duration, not just its latest samples)."""
        rows = self.db.all("SELECT * FROM jf_samples WHERE ts >= ? ORDER BY session_key, ts",
                           ((since - self.play_gap - lookback).isoformat(),))
        plays = group_plays([(r["session_key"], r["item_id"],
                              Sample(datetime.fromisoformat(r["ts"]), _ticks_to_seconds(r["position_ticks"]),
                                     bool(r["is_paused"]), r["audio_lang"])) for r in rows], self.play_gap)
        return [p for p in plays if p.end >= since - self.play_gap]

    async def plugin_rows(self, after_rowid: int) -> list[dict[str, Any]]:
        uid = (await self.user_id()).replace("-", "")
        r = await self.client.post("/user_usage_stats/submit_custom_query", json={
            "CustomQueryString": "SELECT rowid, DateCreated, ItemId, ItemType, ItemName, ClientName, PlayDuration"
                                 f" FROM PlaybackActivity WHERE UserId='{uid}' AND rowid > {int(after_rowid)}"
                                 " ORDER BY rowid",
            "ReplaceUserId": False})
        r.raise_for_status()
        cols = [c.lower() for c in r.json().get("colums") or []]
        if not {"rowid", "datecreated", "itemid", "playduration"} <= set(cols):
            return []
        return [dict(zip(cols, row)) for row in r.json().get("results") or []]

    async def _plugin_backstop(self, now: datetime) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        """Playback Reporting rows the sampler never saw (bridge down) become plays of their own."""
        stored = self.db.get_cursor("jellyfin", "plugin_rowid")
        cursor = int(stored or 0)
        rows = await self.plugin_rows(cursor)
        if stored is None:
            # First check: plays before live tracking started belong to the backfill (Nagare's records, with
            # different IDs), so start after the newest existing row instead of logging them twice.
            self.db.set_cursor("jellyfin", "plugin_rowid", str(max((int(r["rowid"]) for r in rows), default=0)))
            return []
        cutover = self.nagare_until()
        rows = [r for r in rows if not cutover or _dt(r["datecreated"].replace(" ", "T")) >= cutover]
        if not rows:
            return []
        earliest = min(_dt(r["datecreated"].replace(" ", "T")) for r in rows)
        sampled = self.plays(earliest - timedelta(days=1))
        out = []
        settled_upto = cursor
        all_settled = True
        for r in rows:
            started = _dt(r["datecreated"].replace(" ", "T"))
            it = await self.item(r["itemid"])
            runtime = _ticks_to_seconds((it or {}).get("RunTimeTicks"))
            seconds = float(r.get("playduration") or 0)
            seconds = min(seconds, runtime) if runtime else seconds     # an idle-closed session can overcount
            settled = started + timedelta(seconds=seconds) + PLUGIN_SETTLE < now
            if all_settled and settled:
                settled_upto = int(r["rowid"])
            else:
                all_settled = False
            matched = any(p.item_id == r["itemid"] and p.start - PLUGIN_MATCH_SLACK <= started <= p.end + PLUGIN_MATCH_SLACK
                          for p in sampled)
            if matched or now - started < self.play_gap:
                continue   # the sampler measured it (or might still be about to)
            out.append(({"session_id": f"jf:pr:{r['rowid']}", "item_id": r["itemid"], "started_at": started,
                         "updated_at": started + timedelta(seconds=seconds),
                         "seconds": seconds, "quality": "plugin",
                         "flags": ["unsampled"], "title": r.get("itemname"), "series_name": None,
                         "final": settled}, it))
        self.db.set_cursor("jellyfin", "plugin_rowid", str(settled_upto))
        return out

    async def _plugin_item_rows(self, item_id: str, since: datetime) -> list[dict[str, Any]]:
        uid = (await self.user_id()).replace("-", "")
        safe_item = re.sub(r"[^0-9a-fA-F]", "", item_id)
        r = await self.client.post("/user_usage_stats/submit_custom_query", json={
            "CustomQueryString": "SELECT rowid, DateCreated, ItemId, PlayDuration FROM PlaybackActivity"
                                 f" WHERE UserId='{uid}' AND ItemId='{safe_item}'"
                                 f" AND DateCreated >= '{since.strftime('%Y-%m-%d %H:%M:%S')}'",
            "ReplaceUserId": False})
        r.raise_for_status()
        cols = [c.lower() for c in r.json().get("colums") or []]
        return [dict(zip(cols, row)) for row in r.json().get("results") or []] if "datecreated" in cols else []

    async def _played_flags(self, now: datetime) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        """Items newly marked played with no measured play or plugin row around that time: marked watched by
        hand, or watched somewhere the bridge can't see. Runtime estimates, handled by `played_flags`."""
        cursor = _dt(self.db.get_cursor("jellyfin", "played_cursor"))
        if cursor is None:
            self.db.set_cursor("jellyfin", "played_cursor", now.isoformat())
            return []
        uid = await self.user_id()
        data = await self._get(f"/Users/{uid}/Items", Recursive="true", IncludeItemTypes="Episode,Movie",
                               Filters="IsPlayed", SortBy="DatePlayed", SortOrder="Descending", Limit=100,
                               Fields=ITEM_FIELDS + ",SeriesId,RunTimeTicks")
        out = []
        newest = cursor
        sampled = self.plays(cursor - timedelta(days=1))
        for it in data.get("Items") or []:
            last = _dt((it.get("UserData") or {}).get("LastPlayedDate"))
            if last is None or last <= cursor:
                continue
            newest = max(newest, last)
            runtime = _ticks_to_seconds(it.get("RunTimeTicks"))
            window_start = last - timedelta(seconds=runtime) - timedelta(hours=1)
            if any(p.item_id == it["Id"] and p.end >= window_start and p.start <= last + timedelta(minutes=10)
                   for p in sampled):
                continue   # a real, measured play (Jellyfin marks those played too)
            try:
                if await self._plugin_item_rows(it["Id"], window_start):
                    continue   # played while the bridge was down: the plugin backstop logs it
            except httpx.HTTPError:
                pass
            self.db.cache_set(f"jellyfin:item:{it['Id']}", it, now + ITEM_TTL)
            out.append(({"session_id": f"jfplayed:{it['Id']}:{int(last.timestamp())}", "item_id": it["Id"],
                         "started_at": last - timedelta(seconds=runtime), "updated_at": last, "seconds": runtime,
                         "quality": "estimate", "flags": ["estimate", "played_flag"], "title": it.get("Name"),
                         "series_name": it.get("SeriesName"), "final": True}, it))
        self.db.set_cursor("jellyfin", "played_cursor", newest.isoformat())
        return out

    async def poll(self, since: datetime, now: datetime) -> list[Polled]:
        """Every call samples /Sessions; plays that changed since the last poll become events. Playback
        Reporting is checked every `plugin_poll_seconds` for plays the sampler missed."""
        await self.sample(now)
        rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        for p in self.plays(since):
            if p.end < since - timedelta(seconds=1) and now - p.end > self.play_gap + timedelta(minutes=1):
                continue   # unchanged and already final
            it = await self.item(p.item_id)
            rows.append(({"session_id": p.key, "item_id": p.item_id, "started_at": p.start, "updated_at": p.end,
                          "seconds": 0.0, "by_lang": watched_by_language(p.samples, self.play_gap),
                          "quality": "measured", "flags": [], "title": (it or {}).get("Name"), "series_name": None,
                          "final": now - p.end >= self.play_gap}, it))
        last = _dt(self.db.get_cursor("jellyfin", "plugin_checked_at"))
        if last is None or (now - last).total_seconds() >= float(self.src.extra.get("plugin_poll_seconds", 300)):
            try:
                rows += await self._plugin_backstop(now)
            except httpx.HTTPError as e:
                log.warning("Playback Reporting check failed: %s", e)
            try:
                rows += await self._played_flags(now)
            except httpx.HTTPError as e:
                log.warning("played-flag check failed: %s", e)
            self.db.set_cursor("jellyfin", "plugin_checked_at", now.isoformat())
        return await self._build(rows, now, {}) if rows else []

    async def fetch_cover(self, ref: str) -> tuple[bytes, str, str] | None:
        r = await self.client.get(f"/Items/{ref}/Images/Primary", params={"maxHeight": 900})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content, "cover.jpg", r.headers.get("content-type", "image/jpeg")
