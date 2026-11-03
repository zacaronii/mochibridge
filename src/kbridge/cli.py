"""python -m kbridge <command>: see `--help` and the README."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter

from . import config as config_mod
from .db import DB, utcnow
from .kechimochi import Kechimochi
from .service import Bridge, parse_since
from .sources.abs import ABSSource
from .sources.jellyfin import JellyfinSource
from .sources.kavita import KavitaSource
from .sources.youtube import YouTubeSource

LIVE_SOURCES = {"abs": ABSSource, "kavita": KavitaSource, "jellyfin": JellyfinSource, "youtube": YouTubeSource}
ALL_SOURCES = LIVE_SOURCES


def build(cfg: config_mod.Config, db: DB, configured: bool = False) -> Bridge:
    """Live commands use enabled sources; `configured=True` (check/libraries) uses every source with credentials."""
    sources = {}
    for name, cls in (ALL_SOURCES if configured else LIVE_SOURCES).items():
        sc = cfg.source(name)
        if sc.enabled or (configured and sc.url and sc.api_key):
            sources[name] = cls(cfg, db)
    return Bridge(cfg, db, Kechimochi(cfg.kechimochi_url), sources)


async def close(bridge: Bridge) -> None:
    await bridge.km.aclose()
    for s in bridge.sources.values():
        await s.aclose()


async def cmd_check(bridge: Bridge, args) -> int:
    ok = True
    try:
        print(f"kechimochi {bridge.cfg.kechimochi_url}: {await bridge.km.version()}")
    except Exception as e:
        ok = False
        print(f"kechimochi {bridge.cfg.kechimochi_url}: FAILED {e}")
    for name, src in bridge.sources.items():
        try:
            libs = await src.list_libraries()
            print(f"{name}: ok, {len(libs)} libraries")
        except Exception as e:
            ok = False
            print(f"{name}: FAILED {e!r}")
    print(f"mode: {bridge.cfg.mode}")
    return 0 if ok else 1


async def cmd_libraries(bridge: Bridge, args) -> int:
    now = utcnow().isoformat()
    for name, src in bridge.sources.items():
        sc = bridge.cfg.source(name)
        print(f"\n{name}  (default_track={sc.default_track}, languages={sc.languages or 'any'})")
        for lib in await src.list_libraries():
            new = bridge.db.one("SELECT 1 FROM libraries WHERE source=? AND library_id=?", (name, lib.id)) is None
            bridge.db.execute(
                "INSERT INTO libraries(source, library_id, name, media_type, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(source, library_id) DO UPDATE SET name=excluded.name,"
                " media_type=excluded.media_type, last_seen=excluded.last_seen",
                (name, lib.id, lib.name, lib.media_type, now, now))
            eff = sc.library(lib.id, lib.name)
            state = "TRACKED" if eff.track else "off"
            how = "" if eff.explicit else " (default)"
            langs = ",".join(eff.languages) if eff.languages else "any"
            flag = "  NEW" if new else ""
            print(f"  {state:8}{how:10} {lib.name}  [{lib.media_type}] id={lib.id}  "
                  f"lang={langs} unknown={eff.unknown_language} -> {eff.content_type}/{eff.activity_type}{flag}")
    return 0


async def cmd_once(bridge: Bridge, args) -> int:
    now = utcnow()
    since = parse_since(args.since, now) if args.since else None
    for name in bridge.sources:
        if args.source and name != args.source:
            continue
        st = await bridge.poll(name, since=since, now=now)
        print(f"{name}: since {st.since.isoformat(timespec='minutes')}: {st.sessions} sessions, "
              f"{st.logged} loggable ({st.changed} new/changed), {st.waiting} still too short, skipped {dict(st.skipped)}")
    stats = await bridge.sync()
    print(f"sync ({bridge.cfg.mode}): created {stats.media_created} media, updated {stats.media_updated}, "
          f"{stats.covers} covers, {stats.logs_created} logs created, {stats.logs_updated} updated, "
          f"{stats.errors} errors, {stats.dry_ops} new dry-run ops")
    for line in stats.details[: args.show]:
        print("  " + line)
    if len(stats.details) > args.show:
        print(f"  … {len(stats.details) - args.show} more (see `pending`)")
    return 1 if stats.errors else 0


async def cmd_sync(bridge: Bridge, args) -> int:
    """Run the outbox only (no polling)."""
    stats = await bridge.sync()
    print(f"sync ({bridge.cfg.mode}): created {stats.media_created} media, updated {stats.media_updated}, "
          f"{stats.covers} covers, {stats.logs_created} logs created, {stats.logs_updated} updated, "
          f"{stats.errors} errors, {stats.dry_ops} new dry-run ops")
    return 1 if stats.errors else 0


async def cmd_pending(bridge: Bridge, args) -> int:
    db = bridge.db
    print("series:")
    for r in db.all("SELECT source, status, link_method, media_id, title, last_error FROM series_map ORDER BY source, status, title"):
        err = f"  ERROR {r['last_error']}" if r["last_error"] else ""
        print(f"  {r['source']:8} {r['status']:15} {r['link_method'] or '':9} #{r['media_id'] or '-':<5} {r['title']}{err}")
    rows = db.all("SELECT l.*, s.title FROM logs l JOIN series_map s USING(source, series_key)"
                  " WHERE l.synced_hash IS NOT l.desired_json ORDER BY l.started_at")
    print(f"\nlogs not yet in kechimochi: {len(rows)}")
    for r in rows[: args.show]:
        d = json.loads(r["desired_json"])
        print(f"  {d['date']} {d['duration_minutes']:>4}m  {r['title']} — {d['notes']}"
              + (f"  ERROR {r['last_error']}" if r["last_error"] else ""))
    skips = Counter(r["reason"] for r in db.all("SELECT reason FROM skips"))
    print(f"\nskipped sessions: {dict(skips)}")
    print("\nrecent events:")
    for r in reversed(db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.show,))):
        print(f"  {r['ts'][:19]} {r['level']:5} {r['source'] or '':8} {r['message']}")
    return 0


async def cmd_idmap(bridge: Bridge, args) -> int:
    n = await bridge.refresh_idmap(force=True)
    print(f"imported {n} mappings" if n is not None else "anibridge DB not available; kept previous mappings")
    return 0


async def cmd_backfill(bridge: Bridge, args) -> int:
    if not args.preview:
        print("Only `backfill --preview` exists; `migrate` applies the result.")
        return 2
    from .backfill import run_preview
    wanted = args.sources.split(",") if args.sources else None
    rep = await run_preview(bridge.cfg, args.out, wanted, args.nagare_db)
    for s in rep.sources:
        print(f"{s['source']:8} {s['sessions']:5} sessions  {s['included']:4} logs  {s['hours']:7} h  "
              f"{s['earliest']} → {s['latest']}  skipped {s['skipped']}")
    for k, v in rep.planned.items():
        print(f"  {v:4}  {k}")
    print(f"\nwrote {args.out}/backfill_report.html, backfill_preview.csv, summary.md")
    return 0


async def cmd_jellyfin_debug(bridge: Bridge, args) -> int:
    """Watch the user's Jellyfin sessions live: what the client reports and what the bridge would count.
    Read-only (nothing is stored); useful for checking that a client reports pause/seek/audio correctly."""
    from datetime import timedelta
    from .sources.jfplays import Sample, interval_seconds
    src = bridge.sources.get("jellyfin")
    if src is None:
        print("jellyfin is not configured")
        return 2
    gap = src.play_gap  # type: ignore[attr-defined]
    prev: dict[str, Sample] = {}
    total: dict[str, float] = {}
    started = utcnow()
    print(f"sampling every {args.interval}s for {args.minutes} min (Ctrl-C to stop)")
    print(f"{'time':8} {'item':32} {'pos':>8} {'Δpos':>6} {'paused':6} {'audio':5} {'counted':>7}")
    try:
        while utcnow() - started < timedelta(minutes=args.minutes):
            now = utcnow()
            for s in await src.now_playing():  # type: ignore[attr-defined]
                st = s.get("PlayState") or {}
                item = s["NowPlayingItem"]
                langc = await src._stream_language(item["Id"], st.get("AudioStreamIndex"))  # type: ignore[attr-defined]
                cur = Sample(now, (st.get("PositionTicks") or 0) / 10_000_000, bool(st.get("IsPaused")), langc)
                key = f"{s['Id']}:{item['Id']}"
                p = prev.get(key)
                secs = interval_seconds(p, cur, gap) if p else 0.0
                total[key] = total.get(key, 0.0) + secs
                moved = f"{cur.position - p.position:+6.0f}" if p else "     -"
                print(f"{now:%H:%M:%S} {item.get('Name', '')[:32]:32} {cur.position:8.0f} {moved} "
                      f"{str(cur.paused):6} {langc or '?':5} {secs:7.0f}   [{s.get('Client')}]")
                prev[key] = cur
            await asyncio.sleep(args.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    for key, secs in total.items():
        print(f"counted {secs:.0f}s for {key}")
    rows = await src.plugin_rows(0)  # type: ignore[attr-defined]
    recent = [r for r in rows if r["datecreated"] >= started.strftime("%Y-%m-%d %H:%M")[:13]]
    for r in recent:
        print(f"Playback Reporting row {r['rowid']}: {r['datecreated'][:19]} {r['itemname']} "
              f"[{r['clientname']}] PlayDuration={r['playduration']}s")
    return 0


async def cmd_jellyfin_golive(bridge: Bridge, args) -> int:
    """Mark now as the moment live Jellyfin tracking takes over from Nagare's records."""
    value = utcnow().isoformat(timespec="seconds")
    bridge.db.set_cursor("jellyfin", "nagare_until", value)
    bridge.db.event("info", "jellyfin", f"live tracking takes over from Nagare at {value}")
    print(f"nagare_until = {value} (stored in {bridge.cfg.db_path})")
    return 0


async def cmd_health(bridge: Bridge, args) -> int:
    """Container healthcheck: the service loop wrote a heartbeat recently and kechimochi was reachable."""
    from datetime import timedelta
    from .db import parse_iso
    beat = parse_iso(bridge.db.get_cursor("service", "heartbeat"))
    error = bridge.db.get_cursor("service", "last_error")
    if beat is None or utcnow() - beat > timedelta(minutes=2):
        print(f"unhealthy: last heartbeat {beat}")
        return 1
    if error:
        print(f"unhealthy: {error}")
        return 1
    print(f"ok: heartbeat {beat.isoformat(timespec='seconds')}, mode {bridge.cfg.mode}")
    return 0


async def cmd_migrate(bridge: Bridge, args) -> int:
    from pathlib import Path
    from .migrate import Migration, MigrationError, write_markdown
    out = Path(args.out) / utcnow().strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    mig = Migration(bridge.cfg, bridge.db, bridge.km, bridge, nagare_url=args.nagare_url)
    try:
        check = await mig.check(require_nagare_off=not args.skip_nagare_check)
        print(f"preflight: {check}")
        if args.check:
            return 0
        if args.plan:
            linked = {r["media_id"] for r in bridge.db.all("SELECT media_id FROM series_map WHERE media_id IS NOT NULL")}
            if args.linked_from:   # e.g. a backfill preview DB, before the backfill exists in this DB
                import sqlite3
                conn = sqlite3.connect(f"file:{Path(args.linked_from).resolve()}?mode=ro", uri=True)
                linked |= {r[0] for r in conn.execute("SELECT media_id FROM series_map WHERE media_id IS NOT NULL")}
                conn.close()
            plan = await mig.plan(linked)
            (out / "migration_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            write_markdown(out / "migration_plan.md", "Migration plan", plan)
            print(f"plan: delete {len(plan['nagare_logs'])} Nagare logs, {len(plan['manual_deletes'])} manual logs,"
                  f" clean {len(plan['cleanup'])} entries, delete {len(plan['media_deletes'])} empty Nagare entries"
                  f" -> {out}")
            return 0
        # --- apply ---
        if not args.no_backup:
            (out / "kechimochi_full_backup.zip").write_bytes(await bridge.km.export_full_backup())
            import sqlite3
            dst = sqlite3.connect(out / "bridge-before.sqlite")
            bridge.db.conn.backup(dst)
            dst.close()
            print(f"backups written to {out}")
        bridge.cfg.mode = "live"
        report: dict = {"preflight": check, "nagare_until": mig.golive()}
        report["a_nagare_logs_deleted"] = await mig.step_a()
        report["b_manual_logs_deleted"] = await mig.step_b()
        report["c_backfill"] = await mig.step_c(bridge.sources, args.nagare_db)
        report["e_media_deleted"] = await mig.step_e()
        report["d_media_cleaned"] = await mig.step_d()
        report["verify"] = await mig.verify()
        (out / "migration_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        write_markdown(out / "migration_report.md", "Migration report", report)
        v = report["verify"]
        print(f"done: {v['media']} media, {v['logs']} logs, {v['nagare_logs']} Nagare logs left, "
              f"{v['pending_logs']} logs pending -> {out}")
        return 0 if not v["nagare_logs"] and not v["pending_logs"] else 1
    except MigrationError as e:
        print(f"migration stopped: {e}")
        return 1


async def cmd_run(bridge: Bridge, args) -> int:
    """The service: pollers + sync worker, and the web UI on cfg.web_port (same event loop)."""
    import uvicorn
    from .settings import SettingsStore
    from .web.app import create_app
    store = SettingsStore(bridge.cfg.path) if bridge.cfg.path else None
    server = uvicorn.Server(uvicorn.Config(
        create_app(bridge, store), host="0.0.0.0", port=bridge.cfg.web_port, log_level="warning",
        access_log=False, proxy_headers=True, forwarded_allow_ips="*"))
    service = asyncio.create_task(bridge.run_forever())
    try:
        await server.serve()          # returns on SIGTERM/SIGINT
    finally:
        service.cancel()
    return 0


COMMANDS = {"check": cmd_check, "libraries": cmd_libraries, "once": cmd_once, "sync": cmd_sync, "pending": cmd_pending,
            "idmap-refresh": cmd_idmap, "run": cmd_run, "backfill": cmd_backfill,
            "jellyfin-debug": cmd_jellyfin_debug, "jellyfin-golive": cmd_jellyfin_golive, "health": cmd_health,
            "migrate": cmd_migrate}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kbridge")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--db", help="override db_path")
    p.add_argument("--kechimochi", help="override kechimochi.base_url (e.g. the throwaway test instance)")
    p.add_argument("--mode", choices=["dry_run", "live"], help="override mode")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        sp = sub.add_parser(name)
        sp.add_argument("--show", type=int, default=40, help="max lines of detail")
        if name == "backfill":
            sp.add_argument("--preview", action="store_true", help="read history and write a report; no writes")
            sp.add_argument("--out", default="./data/backfill")
            sp.add_argument("--sources", help="comma-separated subset: abs,kavita,jellyfin")
            sp.add_argument("--nagare-db", help="path to (a copy of) Nagare's nagare.sqlite")
        if name == "migrate":
            g = sp.add_mutually_exclusive_group(required=True)
            g.add_argument("--check", action="store_true")
            g.add_argument("--plan", action="store_true")
            g.add_argument("--apply", action="store_true")
            sp.add_argument("--out", default="/data/backups")
            sp.add_argument("--nagare-db", help="Nagare's nagare.sqlite (default: sources.jellyfin.nagare_db)")
            sp.add_argument("--nagare-url", default="http://nagare:9470")
            sp.add_argument("--linked-from", help="(plan) also treat entries mapped in this DB as kept")
            sp.add_argument("--no-backup", action="store_true", help="(rehearsal only) skip the backup ZIP")
            sp.add_argument("--skip-nagare-check", action="store_true", help="(rehearsal only)")
        if name == "jellyfin-debug":
            sp.add_argument("--minutes", type=float, default=10)
            sp.add_argument("--interval", type=float, default=5)
        if name == "once":
            sp.add_argument("--since", help="poll from this point instead of the cursor: 30d, 12h, or ISO time")
            sp.add_argument("--source", choices=["abs", "kavita", "jellyfin", "youtube"])
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from pathlib import Path
    if not Path(args.config).exists():
        print(f"config not found: {args.config} (start from config.example.yaml)", file=sys.stderr)
        return 2
    cfg = config_mod.load(args.config)
    if args.db:
        cfg.db_path = args.db
    if args.kechimochi:
        cfg.kechimochi_url = args.kechimochi.rstrip("/")
    if args.mode:
        cfg.mode = args.mode
    db = DB(":memory:" if args.cmd == "backfill" else cfg.db_path)   # backfill makes its own preview DB
    bridge = build(cfg, db, configured=args.cmd in ("check", "libraries", "backfill", "jellyfin-debug", "migrate"))

    async def go() -> int:
        try:
            return await COMMANDS[args.cmd](bridge, args)
        finally:
            await close(bridge)

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
