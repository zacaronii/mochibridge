"""Push: sessions sent by your own apps to `POST /api/sessions` (bearer `web.api_token`).

Any app that tracks its own sessions (the Genkan launcher, a reader, a script) posts each session again as it
grows. Seconds and characters only ever grow for a session, so a late or repeated post never shrinks a log, and a
session no post has touched for 10 minutes is final (as with YouTube). The app says what it is (`client`), which
works like a library here: per-client rules (track, languages, default content/activity type) go under
`sources.push.libraries.<client>` and show on the Settings page. One kechimochi entry per entry key: the first of
the entry's stable IDs (e.g. `romm.rom_id`), else its title. Those IDs are written into the entry's extra data and
are how an existing entry is matched."""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import filters
from ..config import Config
from ..db import DB, iso, parse_iso, utcnow
from ..models import ACTIVITY_TYPES, CONTENT_TYPES, ActivityEvent, SeriesMeta
from .base import Library, Polled

log = logging.getLogger(__name__)
SESSION_FINAL_AFTER = timedelta(minutes=10)     # the YouTube rule: idle this long and the session is done
CLIENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}(\.[A-Za-z0-9_-]{1,40})?")
MAX_IDS, MAX_FIELDS = 5, 20


def _text(body: dict, name: str, limit: int, required: bool = False) -> str | None:
    value = body.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{name} is required (a string)")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip()[:limit]


def _number(body: dict, name: str, kind: type, required: bool = False):
    value = body.get(name)
    if value is None or value == "":
        if required:
            raise ValueError(f"{name} is required (a number)")
        return None
    try:
        number = None if isinstance(value, bool) else kind(value)
    except (TypeError, ValueError):
        number = None
    if number is None or (kind is float and not math.isfinite(number)) or number < 0 \
            or (kind is int and isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"{name} must be a non-negative {'number' if kind is float else 'integer'}")
    return number


def entry_fields(ids: dict[str, str], fields: dict[str, Any]) -> dict[str, Any]:
    """The entry's extra data: its fields plus its IDs, `romm.rom_id` written as {"romm": {"rom_id": ...}} so the
    resolver can match it later (as `youtube.channel_id` is)."""
    out: dict[str, Any] = dict(fields)
    for path, value in ids.items():
        outer, _, inner = path.partition(".")
        if inner:
            out.setdefault(outer, {})
            if isinstance(out[outer], dict):
                out[outer][inner] = value
        else:
            out[outer] = value
    return out


def series_meta(row, content_type: str, activity_type: str) -> SeriesMeta:
    ids = json.loads(row["entry_ids"] or "{}")
    fields = json.loads(row["entry_fields"] or "{}")
    return SeriesMeta(
        source="push", series_key=row["entry_key"], title=row["title"], library_id=row["client"],
        library_name=row["client"], content_type=content_type, activity_type=activity_type, language=row["language"],
        fields=entry_fields(ids, {"Library": f"Push · {row['client']}", **fields}), match_ids=ids)


class PushSource:
    name = "push"

    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self.src = cfg.source("push")

    async def aclose(self) -> None:
        pass

    async def list_libraries(self) -> list[Library]:
        """Every client that has posted, so it shows up on the Settings page for its rules."""
        return [Library(r["client"], r["client"], "push")
                for r in self.db.all("SELECT DISTINCT client FROM push_sessions ORDER BY client")]

    # ------------------------------------------------------------- input (API)
    def validate(self, body: Any) -> dict[str, Any]:
        """The post, checked and normalised; ValueError (the API answers 400) says what's wrong."""
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        client = _text(body, "client", 40, required=True).lower()
        if not CLIENT_RE.fullmatch(client):
            raise ValueError("client must be 1-40 characters: lowercase letters, digits, - or _")
        session_id = _text(body, "session_id", 120, required=True)
        started_text = _text(body, "started_at", 64, required=True)
        try:
            started = parse_iso(started_text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("started_at must be an ISO 8601 time, e.g. 2026-11-19T20:00:00Z") from None
        if started.tzinfo is None:                  # no offset: the bridge's own time zone
            started = started.replace(tzinfo=ZoneInfo(self.cfg.timezone))
        seconds = _number(body, "seconds", float, required=True)
        characters = _number(body, "characters", int) or 0
        rule = self.src.library(client, client)
        content_type = _text(body, "content_type", 40) or rule.content_type
        activity_type = _text(body, "activity_type", 40) or rule.activity_type
        if content_type not in CONTENT_TYPES:
            raise ValueError(f"content_type must be one of: {', '.join(CONTENT_TYPES)}")
        if activity_type not in ACTIVITY_TYPES:
            raise ValueError(f"activity_type must be one of: {', '.join(ACTIVITY_TYPES)}")
        entry = body.get("entry")
        if not isinstance(entry, dict):
            raise ValueError("entry is required (an object with at least a title)")
        title = _text(entry, "title", 300, required=True)
        ids = entry.get("ids") or {}
        fields = entry.get("fields") or {}
        if not isinstance(ids, dict) or len(ids) > MAX_IDS:
            raise ValueError(f"entry.ids must be an object with at most {MAX_IDS} IDs")
        for k, v in ids.items():
            if not ID_RE.fullmatch(k) or isinstance(v, bool) or not isinstance(v, (str, int)) or not str(v).strip():
                raise ValueError("entry.ids: names like romm.rom_id, values a non-empty string or integer")
        if not isinstance(fields, dict) or len(fields) > MAX_FIELDS:
            raise ValueError(f"entry.fields must be an object with at most {MAX_FIELDS} fields")
        for k, v in fields.items():
            if not isinstance(k, str) or len(k) > 60 or isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise ValueError("entry.fields: short names, values strings or numbers")
        ids = {k: str(v).strip()[:120] for k, v in ids.items()}
        fields = {k: (v[:500] if isinstance(v, str) else v) for k, v in fields.items()}
        # One entry per stable ID when there is one (first in name order), else per title.
        key = next((f"{client}:{k}={ids[k]}" for k in sorted(ids)), f"{client}:title:{title}")
        return {"client": client, "session_id": session_id, "started_at": started, "seconds": seconds,
                "characters": characters, "final": bool(body.get("final")), "content_type": content_type,
                "activity_type": activity_type, "language": (_text(body, "language", 8) or "ja").lower(),
                "quality": body.get("quality") if body.get("quality") in ("measured", "estimate") else "measured",
                "note": _text(body, "note", 200), "title": title, "entry_key": key,
                "entry_ids": json.dumps(ids, ensure_ascii=False), "entry_fields": json.dumps(fields, ensure_ascii=False)}

    def record_session(self, body: Any, now: datetime | None = None) -> None:
        """One post. Max-merge: seconds and characters only grow, and final stays final; the rest follows the
        latest post."""
        now = now or utcnow()
        b = self.validate(body)
        self.db.execute(
            "INSERT INTO push_sessions(client, session_id, started_at, updated_at, seconds, characters, final,"
            " content_type, activity_type, language, quality, note, title, entry_key, entry_ids, entry_fields)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(client, session_id) DO UPDATE SET"
            " seconds=max(push_sessions.seconds, excluded.seconds),"
            " characters=max(push_sessions.characters, excluded.characters),"
            " final=max(push_sessions.final, excluded.final), updated_at=excluded.updated_at,"
            " content_type=excluded.content_type, activity_type=excluded.activity_type, language=excluded.language,"
            " quality=excluded.quality, note=excluded.note, title=excluded.title, entry_key=excluded.entry_key,"
            " entry_ids=excluded.entry_ids, entry_fields=excluded.entry_fields",
            (b["client"], b["session_id"], iso(b["started_at"]), iso(now), b["seconds"], b["characters"],
             int(b["final"]), b["content_type"], b["activity_type"], b["language"], b["quality"], b["note"],
             b["title"], b["entry_key"], b["entry_ids"], b["entry_fields"]))

    # ------------------------------------------------------------- events
    def _event(self, row, now: datetime) -> Polled:
        sid = f"push:{row['client']}:{row['session_id']}"
        meta = series_meta(row, row["content_type"], row["activity_type"])
        started, updated = parse_iso(row["started_at"]), parse_iso(row["updated_at"])
        final = bool(row["final"]) or now - updated >= SESSION_FINAL_AFTER
        chars = int(row["characters"])
        note = row["note"] or row["title"]
        if chars:
            note = f"{note} · {chars:,} characters"
        event = ActivityEvent(source="push", session_id=sid, item_key=meta.series_key, series=meta, note=note,
                              started_at=started, updated_at=updated, duration_seconds=float(row["seconds"]),
                              final=final, language=row["language"], item_title=row["title"],
                              quality=row["quality"], characters=chars)
        rule = self.src.library(row["client"], row["client"])
        decision = filters.evaluate(self.src, rule, row["language"], float(row["seconds"]),
                                    self.cfg.min_session_minutes, final)
        return Polled(sid, meta.series_key, event, decision, f"{row['client']}: {row['title']}")

    async def poll(self, since: datetime, now: datetime) -> list[Polled]:
        """Sessions that changed since the last poll, plus every session not yet final: an open one goes final once
        it has been idle long enough, and that is recorded, so a session that went idle while the bridge was down
        is still finished (logged, or dropped if too short) when it comes back."""
        rows = self.db.all("SELECT * FROM push_sessions WHERE final=0 OR updated_at >= ?",
                           (iso(since - timedelta(seconds=5)),))
        out = []
        for row in rows:
            polled = self._event(row, now)
            out.append(polled)
            self.db.execute("UPDATE push_sessions SET emitted_at=?, final=max(final, ?) WHERE client=? AND session_id=?",
                            (iso(now), int(polled.event.final), row["client"], row["session_id"]))
        return out

    async def history(self, now: datetime) -> list[Polled]:
        return []

    async def fetch_cover(self, ref: str) -> tuple[bytes, str, str] | None:
        return None
