"""Push: sessions sent by your own apps to `POST /api/sessions` (bearer `web.api_token`).

Any app that tracks its own sessions (the Genkan launcher, a reader, a script) posts each session again as it
grows. Seconds and characters only ever grow for a session, so a late or repeated post never shrinks a log, and a
session no post has touched for 10 minutes is final (as with YouTube). The app says what it is (`client`), which
works like a library here: per-client rules (track, languages, default content/activity type) go under
`sources.push.libraries.<client>` and show on the Settings page. One kechimochi entry per entry key: the first of
the entry's stable IDs (e.g. `romm.rom_id`), else its title. Those IDs are written into the entry's extra data and
are how an existing entry is matched.

Covers: an app uploads the entry's image to `POST /api/covers` (kept in `push-covers/` next to the database), or names
a public `entry.cover_url`. Either fills the kechimochi entry's cover only while it is empty. Session posts answer
`"cover": "wanted"` until the bridge has an image or URL for the entry, so an app uploads at most once per entry."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

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
MAX_COVER_BYTES = 5 * 1024 * 1024
COVER_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


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


def image_kind(data: bytes) -> str | None:
    """jpg/png/webp from the file's own header (not the name or content type it came with), else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def parse_entry(client: str, entry: Any) -> dict[str, Any]:
    """An entry object ({title, ids, fields, cover_url}) checked and normalised, with its key."""
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
    cover_url = _text(entry, "cover_url", 2000)
    if cover_url and (urlsplit(cover_url).scheme not in ("http", "https") or not urlsplit(cover_url).netloc):
        raise ValueError("entry.cover_url must be an http(s) URL")
    ids = {k: str(v).strip()[:120] for k, v in ids.items()}
    fields = {k: (v[:500] if isinstance(v, str) else v) for k, v in fields.items()}
    # One entry per stable ID when there is one (first in name order), else per title.
    key = next((f"{client}:{k}={ids[k]}" for k in sorted(ids)), f"{client}:title:{title}")
    return {"title": title, "ids": ids, "fields": fields, "cover_url": cover_url, "key": key}


def parse_client(value: Any) -> str:
    client = _text({"client": value}, "client", 40, required=True).lower()
    if not CLIENT_RE.fullmatch(client):
        raise ValueError("client must be 1-40 characters: lowercase letters, digits, - or _")
    return client


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


def series_meta(row, content_type: str, activity_type: str, cover_ref: str | None = None) -> SeriesMeta:
    ids = json.loads(row["entry_ids"] or "{}")
    fields = json.loads(row["entry_fields"] or "{}")
    return SeriesMeta(
        source="push", series_key=row["entry_key"], title=row["title"], library_id=row["client"],
        library_name=row["client"], content_type=content_type, activity_type=activity_type, language=row["language"],
        fields=entry_fields(ids, {"Library": f"Push · {row['client']}", **fields}), match_ids=ids, cover_ref=cover_ref)


class PushSource:
    name = "push"

    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self.src = cfg.source("push")
        self.covers_dir = Path(self.src.extra.get("covers_dir") or Path(cfg.db_path).parent / "push-covers")
        self._http: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def list_libraries(self) -> list[Library]:
        """Every client that has posted, so it shows up on the Settings page for its rules."""
        return [Library(r["client"], r["client"], "push")
                for r in self.db.all("SELECT DISTINCT client FROM push_sessions ORDER BY client")]

    # ------------------------------------------------------------- input (API)
    def validate(self, body: Any) -> dict[str, Any]:
        """The post, checked and normalised; ValueError (the API answers 400) says what's wrong."""
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        client = parse_client(body.get("client"))
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
        e = parse_entry(client, body.get("entry"))
        return {"client": client, "session_id": session_id, "started_at": started, "seconds": seconds,
                "characters": characters, "final": bool(body.get("final")), "content_type": content_type,
                "activity_type": activity_type, "language": (_text(body, "language", 8) or "ja").lower(),
                "quality": body.get("quality") if body.get("quality") in ("measured", "estimate") else "measured",
                "note": _text(body, "note", 200), "title": e["title"], "entry_key": e["key"],
                "entry_ids": json.dumps(e["ids"], ensure_ascii=False),
                "entry_fields": json.dumps(e["fields"], ensure_ascii=False), "cover_url": e["cover_url"]}

    def record_session(self, body: Any, now: datetime | None = None) -> dict[str, str]:
        """One post. Max-merge: seconds and characters only grow, and final stays final; the rest follows the
        latest post. Returns the answer for the app: whether the bridge still wants a cover for the entry."""
        now = now or utcnow()
        b = self.validate(body)
        self.db.execute(
            "INSERT INTO push_sessions(client, session_id, started_at, updated_at, seconds, characters, final,"
            " content_type, activity_type, language, quality, note, title, entry_key, entry_ids, entry_fields, cover_url)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(client, session_id) DO UPDATE SET"
            " seconds=max(push_sessions.seconds, excluded.seconds),"
            " characters=max(push_sessions.characters, excluded.characters),"
            " final=max(push_sessions.final, excluded.final), updated_at=excluded.updated_at,"
            " content_type=excluded.content_type, activity_type=excluded.activity_type, language=excluded.language,"
            " quality=excluded.quality, note=excluded.note, title=excluded.title, entry_key=excluded.entry_key,"
            " entry_ids=excluded.entry_ids, entry_fields=excluded.entry_fields,"
            " cover_url=coalesce(excluded.cover_url, push_sessions.cover_url)",
            (b["client"], b["session_id"], iso(b["started_at"]), iso(now), b["seconds"], b["characters"],
             int(b["final"]), b["content_type"], b["activity_type"], b["language"], b["quality"], b["note"],
             b["title"], b["entry_key"], b["entry_ids"], b["entry_fields"], b["cover_url"]))
        return {"cover": "set" if self.cover_ref(b["entry_key"]) else "wanted"}

    # ------------------------------------------------------------- covers
    def _cover_path(self, entry_key: str) -> Path | None:
        stem = hashlib.sha1(entry_key.encode()).hexdigest()[:24]
        return next((p for p in (self.covers_dir / f"{stem}.{ext}" for ext in COVER_TYPES) if p.exists()), None)

    def cover_ref(self, entry_key: str) -> str | None:
        """What fetch_cover() should use for the entry: an uploaded image first, else the latest cover_url posted."""
        path = self._cover_path(entry_key)
        if path:
            return f"file:{path.name}"
        row = self.db.one("SELECT cover_url FROM push_sessions WHERE entry_key=? AND cover_url IS NOT NULL"
                          " ORDER BY updated_at DESC LIMIT 1", (entry_key,))
        return f"url:{row['cover_url']}" if row else None

    def store_cover(self, client: Any, entry: Any, data: bytes) -> str:
        """An uploaded cover for an entry (the same entry object as in session posts). Replaces an earlier upload for
        it; it reaches kechimochi only while the entry's cover there is empty."""
        e = parse_entry(parse_client(client), entry)
        if len(data) > MAX_COVER_BYTES:
            raise ValueError(f"image is larger than {MAX_COVER_BYTES // (1024 * 1024)} MB")
        kind = image_kind(data)
        if kind is None:
            raise ValueError("image must be a JPEG, PNG or WebP file")
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        old = self._cover_path(e["key"])
        stem = hashlib.sha1(e["key"].encode()).hexdigest()[:24]
        target = self.covers_dir / f"{stem}.{kind}"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        if old and old != target:
            old.unlink(missing_ok=True)
        # An entry that already exists gets it at the next sync, without waiting for another session post. The sync
        # still fills only an empty cover, so re-arming it never replaces one (it does refill a cover you cleared).
        self.db.execute("UPDATE series_map SET cover_ref=?, cover_done=0 WHERE source='push' AND series_key=?",
                        (f"file:{target.name}", e["key"]))
        log.info("cover stored for %s (%d KB)", e["key"], len(data) // 1024)
        return e["key"]

    # ------------------------------------------------------------- events
    def _event(self, row, now: datetime) -> Polled:
        sid = f"push:{row['client']}:{row['session_id']}"
        meta = series_meta(row, row["content_type"], row["activity_type"], self.cover_ref(row["entry_key"]))
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
        """The image for a cover_ref: an uploaded file, or a cover_url downloaded (size- and type-checked)."""
        kind, _, value = (ref or "").partition(":")
        if kind == "file":
            path = self.covers_dir / Path(value).name
            if not path.exists():
                return None
            data = path.read_bytes()
        elif kind == "url":
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=20, follow_redirects=True,
                                               headers={"User-Agent": "mochibridge (cover fetch)"})
            try:
                async with self._http.stream("GET", value) as r:
                    if r.status_code != 200:
                        log.warning("cover_url %s: HTTP %s", value, r.status_code)
                        return None
                    data = b""
                    async for chunk in r.aiter_bytes():
                        data += chunk
                        if len(data) > MAX_COVER_BYTES:
                            log.warning("cover_url %s: larger than %d bytes", value, MAX_COVER_BYTES)
                            return None
            except httpx.HTTPError as e:
                log.warning("cover_url %s: %s", value, e)
                return None
        else:
            return None
        ext = image_kind(data)
        if ext is None:
            log.warning("cover %s is not a JPEG, PNG or WebP image", ref)
            return None
        return data, f"cover.{ext}", COVER_TYPES[ext]
