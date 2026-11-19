"""Genkan: reading sessions pushed by the Genkan launcher, with active time and characters.

Genkan (a dual-screen launcher and reader for Android handhelds) counts reading by Furoshiki's rules: kana and kanji
only, a line shown again not counted again, active time only while lines or lookups keep coming. It posts each session
to `/api/genkan/session` as it grows; seconds and characters only ever grow for a session, so a late or repeated post
never shrinks a log. A visual novel logs as Visual Novel / Reading (kechimochi counts it in reading speed), any other
game as Videogame / Playing."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .. import filters
from ..config import Config
from ..db import DB, iso, parse_iso, utcnow
from ..models import ActivityEvent, SeriesMeta
from .base import Library, Polled

log = logging.getLogger(__name__)
LIBRARY = Library("genkan", "Genkan", "genkan")
SESSION_FINAL_AFTER = timedelta(minutes=10)     # the YouTube rule: idle this long and the session is done
KINDS = {"vn": ("Visual Novel", "Reading"), "game": ("Videogame", "Playing")}


def series_meta(row) -> SeriesMeta:
    """One kechimochi entry per game: by its RomM rom when Genkan knows it, else by title."""
    content_type, activity_type = KINDS.get(row["kind"], KINDS["game"])
    rom = row["romm_rom_id"]
    key = f"romm:{rom}" if rom else f"title:{row['title']}"
    fields: dict[str, Any] = {"Library": "Genkan"}
    if row["platform"]:
        fields["Platform"] = row["platform"]
    if rom:
        fields["romm"] = {"rom_id": rom}
    return SeriesMeta(
        source="genkan", series_key=key, title=row["title"], library_id=LIBRARY.id, library_name=LIBRARY.name,
        content_type=content_type, activity_type=activity_type, language=row["language"], fields=fields,
        match_ids={"romm.rom_id": str(rom)} if rom else {})


class GenkanSource:
    name = "genkan"

    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self.src = cfg.source("genkan")

    async def aclose(self) -> None:
        pass

    async def list_libraries(self) -> list[Library]:
        return [LIBRARY]

    # ------------------------------------------------------------- input (API)
    def record_session(self, body: dict[str, Any], now: datetime | None = None) -> None:
        """One post from Genkan. Max-merge: seconds and characters only grow, and final stays final."""
        now = now or utcnow()
        kind = body.get("kind") if body.get("kind") in KINDS else "game"
        started = parse_iso(str(body["started_at"])) or now
        self.db.execute(
            "INSERT INTO genkan_sessions(session_id, kind, title, romm_rom_id, platform, started_at, updated_at,"
            " seconds, characters, final, language, quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " seconds=max(genkan_sessions.seconds, excluded.seconds),"
            " characters=max(genkan_sessions.characters, excluded.characters),"
            " final=max(genkan_sessions.final, excluded.final), title=excluded.title, kind=excluded.kind,"
            " updated_at=excluded.updated_at",
            (str(body["session_id"])[:120], kind, str(body["title"])[:300].strip() or "Untitled",
             int(body["romm_rom_id"]) if body.get("romm_rom_id") else None,
             str(body["platform"])[:60] if body.get("platform") else None,
             iso(started), iso(now), max(0.0, float(body["seconds"])), max(0, int(body.get("characters") or 0)),
             1 if body.get("final") else 0, (body.get("language") or "ja")[:8],
             body.get("quality") if body.get("quality") in ("measured", "estimate") else "measured"))

    # ------------------------------------------------------------- events
    def _event(self, row, now: datetime) -> Polled:
        sid = f"genkan:{row['session_id']}"
        meta = series_meta(row)
        started, updated = parse_iso(row["started_at"]), parse_iso(row["updated_at"])
        final = bool(row["final"]) or now - updated >= SESSION_FINAL_AFTER
        chars = int(row["characters"])
        note = f"{row['title']} · {chars:,} characters" if chars else row["title"]
        event = ActivityEvent(source="genkan", session_id=sid, item_key=meta.series_key, series=meta, note=note,
                              started_at=started, updated_at=updated, duration_seconds=float(row["seconds"]),
                              final=final, language=row["language"], item_title=row["title"],
                              quality=row["quality"], characters=chars)
        rule = self.src.library(LIBRARY.id, LIBRARY.name)
        decision = filters.evaluate(self.src, rule, row["language"], float(row["seconds"]),
                                    self.cfg.min_session_minutes, final)
        return Polled(sid, meta.series_key, event, decision, row["title"])

    async def poll(self, since: datetime, now: datetime) -> list[Polled]:
        """Sessions that changed since the last poll, plus ones that just went idle long enough to be final."""
        rows = self.db.all("SELECT * FROM genkan_sessions WHERE updated_at >= ? OR (final=0 AND updated_at >= ?)",
                           (iso(since - timedelta(seconds=5)), iso(now - SESSION_FINAL_AFTER - timedelta(minutes=2))))
        out = []
        for row in rows:
            out.append(self._event(row, now))
            self.db.execute("UPDATE genkan_sessions SET emitted_at=? WHERE session_id=?", (iso(now), row["session_id"]))
        return out

    async def history(self, now: datetime) -> list[Polled]:
        return []

    async def fetch_cover(self, ref: str) -> tuple[bytes, str, str] | None:
        return None
