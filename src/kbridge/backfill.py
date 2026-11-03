"""Backfill preview: read all history through the same pipeline as live sync, into a throwaway
DB, with a kechimochi client that cannot write. Applying the result is `kbridge migrate`."""

from __future__ import annotations

import copy
import dataclasses
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import idmap, report
from .config import Config
from .db import DB, utcnow
from .kechimochi import ReadOnlyKechimochi
from .service import Bridge, PollStats
from .sources.abs import ABSSource
from .sources.base import Source
from .sources.jellyfin import JellyfinSource
from .sources.kavita import KavitaSource

log = logging.getLogger(__name__)
SOURCE_CLASSES = {"abs": ABSSource, "kavita": KavitaSource, "jellyfin": JellyfinSource}


def preview_config(cfg: Config, db_path: Path, wanted: list[str] | None) -> Config:
    """dry_run, its own DB, and every requested source switched on (if it has credentials)."""
    pcfg = dataclasses.replace(cfg, mode="dry_run", db_path=str(db_path), sources=copy.deepcopy(cfg.sources))
    for name, sc in pcfg.sources.items():
        sc.enabled = bool(sc.url and sc.api_key) and (wanted is None or name in wanted)
    # `kbridge jellyfin-golive` records the Nagare -> live cut-over in the live DB; the preview must honour it.
    live_db = Path(cfg.db_path)
    if live_db.exists() and live_db.resolve() != Path(db_path).resolve():
        conn = sqlite3.connect(f"{live_db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM cursors WHERE source='jellyfin' AND key='nagare_until'").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if row and row[0]:
            pcfg.sources["jellyfin"].extra["nagare_until"] = row[0]
    return pcfg


def fresh_db(path: Path) -> DB:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    return DB(path)


async def run_preview(cfg: Config, out_dir: str | Path, wanted: list[str] | None = None,
                      nagare_db: str | None = None) -> report.Report:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pcfg = preview_config(cfg, out / "preview.sqlite", wanted)
    db = fresh_db(out / "preview.sqlite")
    km = ReadOnlyKechimochi(pcfg.kechimochi_url)
    sources: dict[str, Source] = {n: SOURCE_CLASSES[n](pcfg, db) for n, sc in pcfg.sources.items() if sc.enabled}
    bridge = Bridge(pcfg, db, km, sources)
    try:
        idmap.refresh(db, pcfg.idmap_db, force=True)
        now = utcnow()
        stats: dict[str, PollStats] = {}
        for name, src in sources.items():
            log.info("reading %s history", name)
            if name == "jellyfin":
                polled = await src.history(now, nagare_db=nagare_db)   # type: ignore[call-arg]
            else:
                polled = await src.history(now)
            st = PollStats(name, datetime(1970, 1, 1, tzinfo=timezone.utc))
            st.sessions = len(polled)
            bridge.resolver.invalidate()
            for p in polled:
                await bridge.ingest(p, st)
            stats[name] = st
            log.info("%s: %d sessions, %d loggable, skipped %s", name, st.sessions, st.logged, st.skipped)
        sync_stats = await bridge.sync()      # dry_run: records planned requests in `ops`
        rep = await report.build(db, km, pcfg, stats, sync_stats)
        report.write(rep, out)
        return rep
    finally:
        await km.aclose()
        for s in sources.values():
            await s.aclose()
