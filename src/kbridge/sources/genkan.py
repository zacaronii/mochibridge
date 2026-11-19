"""Genkan: reading sessions pushed by the Genkan launcher, with active time and characters.

Genkan (a dual-screen launcher and reader for Android handhelds) counts reading by Furoshiki's rules: kana and kanji
only, a line shown again not counted again, active time only while lines or lookups keep coming. It posts each session
to `/api/genkan/session` as it grows; seconds and characters only ever grow for a session, so a late or repeated post
never shrinks a log. A visual novel logs as Visual Novel / Reading (kechimochi counts it in reading speed), any other
game as Videogame / Playing."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
    def validate(self, body: Any) -> dict[str, Any]:
        """The post, checked and normalised; ValueError (the API answers 400) says what's wrong."""
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        for name in ("session_id", "title", "started_at"):
            if not isinstance(body.get(name), str) or not body[name].strip():
                raise ValueError(f"{name} is required (a string)")
        for name in ("kind", "platform", "language", "quality"):
            if body.get(name) is not None and not isinstance(body[name], str):
                raise ValueError(f"{name} must be a string")
        try:
            started = parse_iso(body["started_at"].strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("started_at must be an ISO 8601 time, e.g. 2026-11-19T20:00:00Z") from None
        if started.tzinfo is None:                  # no offset: the bridge's own time zone
            started = started.replace(tzinfo=ZoneInfo(self.cfg.timezone))
        numbers = {}
        for name, kind_, required in (("seconds", float, True), ("characters", int, False), ("romm_rom_id", int, False)):
            value = body.get(name)
            if value is None or value == "":
                if required:
                    raise ValueError(f"{name} is required (a number)")
                numbers[name] = None
                continue
            try:
                number = kind_(value) if not isinstance(value, bool) else None
            except (TypeError, ValueError):
                number = None
            if number is None or (kind_ is float and not math.isfinite(number)) or number < 0 \
                    or (kind_ is int and isinstance(value, float) and not value.is_integer()):
                raise ValueError(f"{name} must be a non-negative {'number' if kind_ is float else 'integer'}")
            numbers[name] = number
        return {"session_id": body["session_id"].strip()[:120], "title": body["title"].strip()[:300] or "Untitled",
                "kind": body.get("kind") if body.get("kind") in KINDS else "game", "started_at": started,
                "seconds": numbers["seconds"], "characters": numbers["characters"] or 0,
                "romm_rom_id": numbers["romm_rom_id"] or None,
                "platform": (body.get("platform") or "")[:60] or None, "final": bool(body.get("final")),
                "language": (body.get("language") or "ja")[:8],
                "quality": body.get("quality") if body.get("quality") in ("measured", "estimate") else "measured"}

    def record_session(self, body: dict[str, Any], now: datetime | None = None) -> None:
        """One post from Genkan. Max-merge: seconds and characters only grow, and final stays final."""
        now = now or utcnow()
        b = self.validate(body)
        self.db.execute(
            "INSERT INTO genkan_sessions(session_id, kind, title, romm_rom_id, platform, started_at, updated_at,"
            " seconds, characters, final, language, quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " seconds=max(genkan_sessions.seconds, excluded.seconds),"
            " characters=max(genkan_sessions.characters, excluded.characters),"
            " final=max(genkan_sessions.final, excluded.final), title=excluded.title, kind=excluded.kind,"
            " updated_at=excluded.updated_at",
            (b["session_id"], b["kind"], b["title"], b["romm_rom_id"], b["platform"], iso(b["started_at"]), iso(now),
             b["seconds"], b["characters"], int(b["final"]), b["language"], b["quality"]))

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
        """Sessions that changed since the last poll, plus every session not yet final: an open one goes final once
        it has been idle long enough, and that is recorded, so a session that went idle while the bridge was down
        is still finished (logged, or dropped if too short) when it comes back."""
        rows = self.db.all("SELECT * FROM genkan_sessions WHERE final=0 OR updated_at >= ?",
                           (iso(since - timedelta(seconds=5)),))
        out = []
        for row in rows:
            polled = self._event(row, now)
            out.append(polled)
            self.db.execute("UPDATE genkan_sessions SET emitted_at=?, final=max(final, ?) WHERE session_id=?",
                            (iso(now), int(polled.event.final), row["session_id"]))
        return out

    async def history(self, now: datetime) -> list[Polled]:
        return []

    async def fetch_cover(self, ref: str) -> tuple[bytes, str, str] | None:
        return None
