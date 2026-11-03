"""Cross-database anime IDs (AniDB/TVDB/TMDB/IMDB/MAL -> AniList, …) imported from anibridge's mapping DB.

anibridge keeps `animap_entry(provider, entry_id, entry_scope)` and `animap_mapping(source_entry_id,
destination_entry_id, source_range, destination_range)` up to date. We copy those rows into our own
`idmap` table so lookups keep working if anibridge is down or its DB is mid-write.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

from .db import DB, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

_EXPORT_SQL = """
SELECT s.provider, s.entry_id, s.entry_scope, d.provider, d.entry_id, d.entry_scope,
       m.source_range, m.destination_range
FROM animap_mapping m
JOIN animap_entry s ON s.id = m.source_entry_id
JOIN animap_entry d ON d.id = m.destination_entry_id
"""


def _open_readonly(path: str) -> sqlite3.Connection:
    """Never write to anibridge's files: plain read-only first (sees the WAL), immutable as a fallback
    for read-only mounts where SQLite can't use the -shm file."""
    uri = Path(path).resolve().as_uri()
    try:
        conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
        conn.execute("SELECT count(*) FROM animap_entry").fetchone()
        return conn
    except sqlite3.Error:
        return sqlite3.connect(f"{uri}?immutable=1", uri=True)


def refresh(db: DB, anibridge_db: str | None, max_age_hours: float = 24, force: bool = False) -> int | None:
    """Re-import if the last import is older than max_age_hours. Returns rows imported, or None if skipped."""
    if not anibridge_db or not Path(anibridge_db).exists():
        if anibridge_db:
            log.warning("anibridge DB %s not found; keeping previous ID mappings", anibridge_db)
        return None
    last = parse_iso(db.get_cursor("idmap", "imported_at"))
    if not force and last and utcnow() - last < timedelta(hours=max_age_hours):
        return None
    src = _open_readonly(anibridge_db)
    try:
        rows = src.execute(_EXPORT_SQL).fetchall()
    finally:
        src.close()
    if not rows:
        log.warning("anibridge DB has no mappings; keeping previous ones")
        return 0
    with db.tx():
        db.execute("DELETE FROM idmap")
        db.conn.executemany(
            "INSERT INTO idmap(src_provider, src_id, src_scope, dst_provider, dst_id, dst_scope,"
            " src_range, dst_range) VALUES (?,?,?,?,?,?,?,?)", rows)
        db.set_cursor("idmap", "imported_at", iso(utcnow()))
    log.info("imported %d ID mappings from anibridge", len(rows))
    return len(rows)


def _range_start(r: str | None) -> int:
    try:
        return int(str(r).split(",")[0].split("-")[0])
    except (TypeError, ValueError):
        return 10**9


def lookup(db: DB, provider: str, entry_id: str | int, scope: str | None = None) -> dict[str, str]:
    """IDs for the same work in other databases, e.g. lookup(db, "anidb", 23) -> {"anilist": "1", …}.

    AniDB entries have scopes R (regular episodes), S (specials) and O (other); TVDB/TMDB shows use
    season scopes ("s1"). With scope=None we prefer R, then the mapping that starts earliest, so a
    series resolves to its main entry rather than a special or a later split cour."""
    rows = db.all("SELECT * FROM idmap WHERE src_provider=? AND src_id=?", (provider, str(entry_id)))
    if scope is not None:
        rows = [r for r in rows if r["src_scope"] == scope]
    elif any(r["src_scope"] == "R" for r in rows):
        rows = [r for r in rows if r["src_scope"] == "R"]
    best: dict[str, tuple[tuple[int, int], str]] = {}
    for r in rows:
        dst = r["dst_provider"]
        rank = (_range_start(r["src_range"]), _range_start(r["dst_range"]))
        if dst not in best or rank < best[dst][0]:
            best[dst] = (rank, r["dst_id"] if not r["dst_scope"] else f"{r['dst_id']}:{r['dst_scope']}")
    return {dst: v for dst, (_, v) in best.items()}


def anilist_id(db: DB, provider: str, entry_id: str | int) -> str | None:
    return lookup(db, provider, entry_id).get("anilist")
