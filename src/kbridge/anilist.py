"""AniList titles (native / romaji / English / synonyms) used as alternate titles when matching entries.

Only public AniList IDs are sent. Results are cached for 30 days; if AniList is unreachable we simply
match without them."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from .db import DB, utcnow

log = logging.getLogger(__name__)
URL = "https://graphql.anilist.co"
TTL = timedelta(days=30)
BATCH = 50
QUERY = "query ($ids: [Int]) { Page(perPage: 50) { media(id_in: $ids) { id title { romaji english native } synonyms } } }"


async def titles(db: DB, ids: list[str | int], client: httpx.AsyncClient | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    missing = []
    for i in {str(i) for i in ids if i}:
        cached = db.cache_get(f"anilist:titles:{i}")
        if cached is not None:
            out[i] = cached
        else:
            missing.append(i)
    if not missing:
        return out
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        for start in range(0, len(missing), BATCH):
            chunk = missing[start:start + BATCH]
            r = await client.post(URL, json={"query": QUERY, "variables": {"ids": [int(i) for i in chunk]}})
            r.raise_for_status()
            for m in r.json()["data"]["Page"]["media"]:
                t = m.get("title") or {}
                names = [t.get("native"), t.get("romaji"), t.get("english"), *(m.get("synonyms") or [])]
                out[str(m["id"])] = [n for n in dict.fromkeys(names) if n]
            for i in chunk:   # cache misses too, so unknown IDs aren't re-queried every run
                db.cache_set(f"anilist:titles:{i}", out.get(i, []), utcnow() + TTL)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("AniList title lookup failed (%s); matching without AniList titles", e)
    finally:
        if own:
            await client.aclose()
    return out
