"""Map a source series to a kechimochi media entry: existing mapping, override, auto-link, or create."""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import unicodedata
from typing import Any

from . import extra, lang
from .config import Config
from .db import DB, iso, utcnow
from .kechimochi import Kechimochi
from .models import SeriesMeta

log = logging.getLogger(__name__)

_SEASON_RE = re.compile(
    r"(\s*[\(\[]?\s*(season|series|part|cour)\s*\d+\s*[\)\]]?"
    r"|\s*\d+(st|nd|rd|th)\s+season"
    r"|\s*第\s*\d+\s*[期章部]"
    r"|\s+s\d{1,2})\s*$", re.IGNORECASE)


# Trailing tags that name a release rather than a work: "(TV)", "(2012)", "[BD]", "(Uncensored)".
_QUALIFIER_RE = re.compile(
    r"\s*[\(\[](?:tv|bd|dvd|web|ova|ona|oad|movie|film|uncensored|dub(?:bed)?|sub(?:bed)?|"
    r"tv series|(?:19|20)\d{2})[\)\]]\s*$", re.IGNORECASE)


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower().strip()
    for _ in range(3):   # "Title (2012) (TV)"
        stripped = _SEASON_RE.sub("", _QUALIFIER_RE.sub("", t)).strip()
        if stripped == t:
            break
        t = stripped
    return "".join(ch for ch in t if unicodedata.category(ch)[0] not in "PSZC")


def similarity(a: str, b: str) -> tuple[float, str] | None:
    """Score two *normalised* titles that are close but not equal. Never used to auto-link."""
    if not a or not b or a == b:
        return None
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and short in long_ and len(short) / len(long_) >= 0.4:
        return round(0.8 + 0.2 * len(short) / len(long_), 3), "contains"
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio >= 0.85:
        return round(ratio, 3), "similar"
    return None


def stable_hash(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def desired_media(meta: SeriesMeta) -> dict[str, Any]:
    """What the bridge wants on the entry: the full payload for a new entry, and the source of
    fill-empty values (description, extra_data fields) for an existing one."""
    return {
        "title": meta.title,
        "variant": "",
        "default_activity_type": meta.activity_type,
        "status": "Active",
        "language": lang.display_name(meta.language),
        "description": meta.description or "",
        "cover_image": "",
        "content_type": meta.content_type,
        "tracking_status": "Ongoing",
        "fields": extra.desired_fields(meta),
    }


def _extra_id(media: dict[str, Any], outer: str, inner: str) -> str | None:
    value = next((v for k, v in extra.parse(media.get("extra_data")).items() if k.lower() == outer.lower()), None)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return str(value.get(inner)) if isinstance(value, dict) and value.get(inner) else None


def narrow_by_type(hits: list[dict[str, Any]], content_type: str) -> list[dict[str, Any]]:
    same = [m for m in hits if m.get("content_type") == content_type]
    return same if len(hits) > 1 and len(same) == 1 else hits


def _nagare_ids(media: dict[str, Any]) -> set[str]:
    data = extra.parse(media.get("extra_data"))
    blob = next((v for k, v in data.items() if k.lower() == "nagare"), None)
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except ValueError:
            return set()
    if not isinstance(blob, dict):
        return set()
    ids = set()
    for h in blob.get("history") or []:
        hid = str(h.get("history_id", ""))
        ids.add(re.sub(r"^rewatch:\d+:", "", hid))
    return ids


class Resolver:
    def __init__(self, db: DB, km: Kechimochi, cfg: Config):
        self.db, self.km, self.cfg = db, km, cfg
        self._media: list[dict[str, Any]] | None = None

    def invalidate(self) -> None:
        """Call once per poll cycle so new series see entries created since."""
        self._media = None

    async def media(self) -> list[dict[str, Any]]:
        if self._media is None:
            self._media = await self.km.list_media()
        return self._media

    def _override(self, meta: SeriesMeta) -> tuple[str, int | None] | None:
        def match(o: dict[str, Any]) -> bool:
            return o.get("source") == meta.source and str(o.get("series_key")) == meta.series_key
        if any(match(o) for o in self.cfg.override_ignore):
            return ("ignored", None)
        for o in self.cfg.override_map:
            if match(o):
                return ("override", int(o["media_id"]))
        return None

    async def candidates(self, meta: SeriesMeta) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        """Pick the entry to link. Returns (method, hits, merges): one hit means link it; several mean the
        user has to choose; `merges` are Nagare-created duplicates of the chosen entry (merged by the migration).

        Preference: the user's own entry with a matching title, then the Nagare entry whose marker points at
        this series (the one with the matching title if Nagare made several), then any single title match.
        Ties between title matches are broken by content type (a light novel prefers the Novel entry)."""
        media = await self.media()
        for path, value in meta.match_ids.items():       # stable IDs first (e.g. YouTube channel ID)
            outer, _, inner = path.partition(".")
            hits = [m for m in media if value and _extra_id(m, outer, inner) == value]
            if hits:
                return "id", narrow_by_type(hits, meta.content_type), []
        keys = self._title_keys(meta)
        wanted = set(meta.nagare_history_ids)
        nagare_hits = [m for m in media if wanted and _nagare_ids(m) & wanted]
        title_hits = [m for m in media if normalize_title(m["title"]) in keys]

        def narrow(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
            same_type = [m for m in hits if m.get("content_type") == meta.content_type]
            return same_type if len(hits) > 1 and len(same_type) == 1 else hits

        user_hits = narrow([m for m in title_hits if not _nagare_ids(m)])
        if user_hits:
            # Duplicates to merge: Nagare entries whose marker points at this series, or same-type title matches
            # (an anime adaptation is not a duplicate of the light novel with the same title).
            same_work = nagare_hits + [m for m in title_hits if m.get("content_type") == meta.content_type]
            nagare_dupes = [m for m in {m["id"]: m for m in same_work}.values()
                            if _nagare_ids(m) and m["id"] not in {u["id"] for u in user_hits}]
            return "title", user_hits, nagare_dupes if len(user_hits) == 1 else []
        if nagare_hits:
            if len(nagare_hits) == 1:
                return "nagare", nagare_hits, []
            primary = [m for m in nagare_hits if normalize_title(m["title"]) == normalize_title(meta.title)]
            titled = primary if len(primary) == 1 else narrow(
                [m for m in nagare_hits if normalize_title(m["title"]) in keys])
            if len(titled) == 1:
                return "nagare", titled, [m for m in nagare_hits if m["id"] != titled[0]["id"]]
            return "nagare", nagare_hits, []
        return "title", narrow(title_hits), []

    @staticmethod
    def _title_keys(meta: SeriesMeta) -> set[str]:
        keys = {normalize_title(t) for t in [meta.title, *meta.alt_titles] if t}
        keys.discard("")
        return keys

    async def suggest(self, meta: SeriesMeta, limit: int = 3) -> list[tuple[dict[str, Any], float, str]]:
        """Entries whose titles are close to this series. Stored for review; never linked automatically."""
        keys = self._title_keys(meta)
        found: dict[int, tuple[dict[str, Any], float, str]] = {}
        for m in await self.media():
            nt = normalize_title(m["title"])
            for k in keys:
                s = similarity(k, nt)
                if s and (m["id"] not in found or s[0] > found[m["id"]][1]):
                    found[m["id"]] = (m, *s)
        best = sorted(found.values(), key=lambda x: -x[1])[:limit]
        self.db.execute("DELETE FROM suggestions WHERE source=? AND series_key=?", (meta.source, meta.series_key))
        for m, score, method in best:
            self.db.execute("INSERT INTO suggestions(source, series_key, media_id, title, score, method)"
                            " VALUES (?,?,?,?,?,?)", (meta.source, meta.series_key, m["id"], m["title"], score, method))
        return best

    async def ensure(self, meta: SeriesMeta) -> str:
        """Make sure series_map has a row for this series; return its status."""
        desired = desired_media(meta)
        row = self.db.one("SELECT * FROM series_map WHERE source=? AND series_key=?",
                          (meta.source, meta.series_key))
        override = self._override(meta)
        now = iso(utcnow())

        if row is not None:
            status, media_id, method = row["status"], row["media_id"], row["link_method"]
            if override:
                wanted = ("ignored", None) if override[0] == "ignored" else ("linked", override[1])
                if (status, media_id) != wanted:
                    status, media_id = wanted
                    method = "override"
                    self.db.event("info", meta.source,
                                  f"override applied to {meta.title!r}: {status} {media_id or ''}")
            self.db.execute(
                "UPDATE series_map SET status=?, media_id=?, link_method=?, title=?, desired_json=?, cover_ref=?,"
                " updated_at=? WHERE source=? AND series_key=?",
                (status, media_id, method, meta.title, json.dumps(desired, ensure_ascii=False),
                 meta.cover_ref, now, meta.source, meta.series_key))
            return status

        if override:
            status, media_id, method = ("ignored", None, "override") if override[0] == "ignored" \
                else ("linked", override[1], "override")
        else:
            method, hits, merges = await self.candidates(meta)
            if len(hits) == 1:
                status, media_id = "linked", hits[0]["id"]
                self.db.event("info", meta.source,
                              f"linked {meta.title!r} to existing entry #{media_id} {hits[0]['title']!r} ({method})")
                self.db.execute("DELETE FROM suggestions WHERE source=? AND series_key=?", (meta.source, meta.series_key))
                for m in merges:
                    self.db.execute("INSERT OR REPLACE INTO suggestions(source, series_key, media_id, title, score, method)"
                                    " VALUES (?,?,?,?,?,?)", (meta.source, meta.series_key, m["id"], m["title"], 1.0, "merge"))
                if merges:
                    self.db.event("info", meta.source, f"Nagare duplicates of #{media_id} to merge later: "
                                  + ", ".join(f"#{m['id']} {m['title']!r}" for m in merges))
            elif len(hits) > 1:
                status, media_id = "needs_mapping", None
                ids = ", ".join(f"#{h['id']} {h['title']!r}" for h in hits)
                self.db.event("warn", meta.source, f"{meta.title!r} matches several entries ({method}): {ids}")
            else:
                status, media_id, method = "pending_create", None, None
                if await self.suggest(meta):
                    self.db.event("info", meta.source, f"{meta.title!r} has possible matches; see suggestions")
        self.db.execute(
            "INSERT INTO series_map(source, series_key, media_id, status, link_method, title, desired_json,"
            " cover_ref, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (meta.source, meta.series_key, media_id, status, method, meta.title,
             json.dumps(desired, ensure_ascii=False), meta.cover_ref, now))
        return status
