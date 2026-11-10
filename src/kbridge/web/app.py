"""The bridge's web UI: status, review queue, activity, mappings and settings.

Runs inside the bridge process (same asyncio loop as the pollers), so actions apply immediately. Behind
a LAN-only reverse proxy; every POST also needs a same-origin request and the per-process token."""

from __future__ import annotations

import json
import os
import secrets
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import settings
from ..config import _INHERIT, ConfigError
from ..db import iso, parse_iso, utcnow
from ..service import Bridge
from ..sources.jfplays import watched_by_language

HERE = Path(__file__).parent
CONTENT_TYPES = ["Anime", "Movie", "Drama", "Manga", "Novel", "WebNovel", "NonFiction", "Audio", "Visual Novel",
                 "Videogame", "Livestream", "Youtube Video", "Unknown"]
ACTIVITIES = ["Watching", "Reading", "Listening", "Playing", "None"]
REASONS = {"played_flag_only": "Marked played, no watch data", "generated": "Kavita generated session",
           "language:unknown": "Unknown language", "long_session": "Very long session", "manual": "Manual"}


def create_app(bridge: Bridge, store: settings.SettingsStore | None) -> FastAPI:
    app = FastAPI(title="mochibridge", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    token = secrets.token_urlsafe(24)
    lib_cache: dict[str, tuple[datetime, list]] = {}

    def tz() -> ZoneInfo:
        return ZoneInfo(bridge.cfg.timezone)

    def local(ts: str | None, fmt: str = "%m-%d %H:%M") -> str:
        dt = parse_iso(ts) if ts else None
        return dt.astimezone(tz()).strftime(fmt) if dt else ""

    def ago(ts: str | None) -> str:
        dt = parse_iso(ts) if ts else None
        if not dt:
            return "never"
        s = int((utcnow() - dt).total_seconds())
        if s < 90:
            return f"{s}s ago"
        if s < 5400:
            return f"{s // 60} min ago"
        if s < 172800:
            return f"{s // 3600} h ago"
        return f"{s // 86400} d ago"

    templates.env.filters.update(local=local, ago=ago, reason=lambda r: REASONS.get(r, r))
    templates.env.globals.update(token=token)

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.update(request=request, cfg=bridge.cfg, msg=request.query_params.get("msg"),
                   err=request.query_params.get("err"), page=name.split(".")[0],
                   pending_reviews=bridge.db.one("SELECT count(*) n FROM reviews WHERE status='pending'")["n"])
        return templates.TemplateResponse(request, name, ctx)

    def back(path: str, **q: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?{urlencode(q)}" if q else path, status_code=303)

    def check(request: Request, form_token: str) -> None:
        """Same-origin + token. The proxy's allowlist is the outer fence; this stops cross-site form posts."""
        if not secrets.compare_digest(form_token or "", token):
            raise HTTPException(403, "invalid form token (reload the page)")
        origin = request.headers.get("origin") or request.headers.get("referer")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if not origin or urlparse(origin).netloc != host:
            raise HTTPException(403, "cross-origin request refused")

    def api_auth(request: Request) -> None:
        """Bearer token for the extension and the iOS Shortcut (they can't use the form token)."""
        expected = bridge.cfg.web_api_token
        if not expected:
            raise HTTPException(503, "API disabled: set web.api_token in config.yaml")
        got = request.headers.get("authorization", "")
        if not secrets.compare_digest(got.removeprefix("Bearer ").strip(), expected):
            raise HTTPException(401, "invalid API token")

    def youtube():
        src = bridge.sources.get("youtube")
        if src is None:
            raise HTTPException(503, "the YouTube source is not enabled")
        return src

    def save(*mutators) -> str | None:
        if store is None:
            return "settings are read-only (no config file)"
        try:
            bridge.apply_config(store.update(*mutators))
        except ConfigError as e:
            return str(e)
        return None

    # ------------------------------------------------------------- pages
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "mode": bridge.cfg.mode}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        db = bridge.db
        today = utcnow().astimezone(tz()).date()
        week = (today - timedelta(days=6)).isoformat()
        sources = []
        for name in ("jellyfin", "kavita", "abs", "youtube"):
            sc = bridge.cfg.source(name)
            if name == "youtube" and not sc.enabled:
                continue
            mins = {"today": 0, "week": 0}
            for r in db.all("SELECT desired_json FROM logs WHERE source=? AND log_id IS NOT NULL", (name,)):
                d = json.loads(r["desired_json"])
                if d["date"] >= week:
                    mins["week"] += d["duration_minutes"]
                    if d["date"] == today.isoformat():
                        mins["today"] += d["duration_minutes"]
            sources.append({
                "name": name, "enabled": sc.enabled and name in bridge.sources,
                "polled": db.get_cursor(name, "polled_until"), **mins,
                "pending": db.one("SELECT count(*) n FROM logs WHERE source=? AND synced_hash IS NOT desired_json",
                                  (name,))["n"],
                "errors": db.one("SELECT count(*) n FROM logs WHERE source=? AND last_error IS NOT NULL", (name,))["n"]
                + db.one("SELECT count(*) n FROM series_map WHERE source=? AND last_error IS NOT NULL", (name,))["n"],
            })
        try:
            version = await bridge.km.version()
        except Exception as e:   # shown, not fatal
            version = f"unreachable ({e.__class__.__name__})"
        playing = []
        jf = bridge.sources.get("jellyfin")
        if jf is not None:
            now = utcnow()
            for p in jf.plays(now - timedelta(minutes=2)):   # type: ignore[attr-defined]
                if now - p.end > timedelta(seconds=60):
                    continue
                it = await jf.item(p.item_id) or {}       # type: ignore[attr-defined]
                last = p.samples[-1]
                by_lang = watched_by_language(p.samples, jf.play_gap)   # type: ignore[attr-defined]
                playing.append({"title": it.get("SeriesName") or it.get("Name") or p.item_id,
                                "episode": it.get("Name") if it.get("SeriesName") else "",
                                "position": int(last.position), "runtime": int((it.get("RunTimeTicks") or 0) / 1e7),
                                "paused": last.paused, "audio": last.audio_lang,
                                "counted": int(sum(v for k, v in by_lang.items()))})
        recent = []
        for r in db.all("SELECT l.*, s.title, s.media_id FROM logs l JOIN series_map s USING(source, series_key)"
                        " WHERE l.log_id IS NOT NULL ORDER BY l.updated_at DESC LIMIT 15"):
            d = json.loads(r["desired_json"])
            recent.append({**d, "source": r["source"], "title": r["title"], "media_id": r["media_id"]})
        return render(request, "dashboard.html", sources=sources, version=version, playing=playing, recent=recent,
                      youtube=youtube_status() if "youtube" in bridge.sources else None,
                      heartbeat=db.get_cursor("service", "heartbeat"), last_error=db.get_cursor("service", "last_error"),
                      events=db.all("SELECT * FROM events ORDER BY id DESC LIMIT 25"))

    @app.get("/review", response_class=HTMLResponse)
    async def review(request: Request):
        groups: dict[str, list] = defaultdict(list)
        for r in bridge.db.all("SELECT * FROM reviews WHERE status='pending' ORDER BY created_at DESC"):
            groups[r["reason"]].append({**dict(r), "meta": json.loads(r["meta_json"] or "{}")})
        decided = bridge.db.all("SELECT * FROM reviews WHERE status!='pending' ORDER BY decided_at DESC LIMIT 20")
        return render(request, "review.html", groups=dict(groups),
                      decided=[{**dict(r), "meta": json.loads(r["meta_json"] or "{}")} for r in decided])

    @app.post("/review")
    async def review_action(request: Request, token_: str = Form(alias="token"), action: str = Form("")):
        check(request, token_)
        form = await request.form()
        keys = form.getlist("key")
        if form.get("row"):                 # a single row's own button: only that row
            row = json.loads(str(form["row"]))
            keys, action = [json.dumps(row["key"])], row["action"]
        done = 0
        for key in keys:
            source, session_id, item_key = json.loads(key)
            if action == "approve":
                done += await bridge.approve(source, session_id, item_key)
            else:
                done += bridge.reject(source, session_id, item_key)
        verb = "approved" if action == "approve" else "rejected"
        return back(form.get("next") or "/review", msg=f"{verb} {done} session(s)")

    @app.get("/activity", response_class=HTMLResponse)
    async def activity(request: Request, source: str = "", reason: str = "", show: str = "logs"):
        db = bridge.db
        where, args = [], []
        if source:
            where.append("l.source=?")
            args.append(source)
        logs = []
        for r in db.all("SELECT l.*, s.title, s.media_id, s.status AS series_status FROM logs l"
                        " JOIN series_map s USING(source, series_key)"
                        + (" WHERE " + " AND ".join(where) if where else "")
                        + " ORDER BY l.started_at DESC LIMIT 300", args):
            d = json.loads(r["desired_json"])
            status = ("error" if r["last_error"] else "synced" if r["synced_hash"] == r["desired_json"]
                      else "waiting for mapping" if r["series_status"] == "needs_mapping" else "pending")
            logs.append({**d, "source": r["source"], "title": r["title"], "status": status, "error": r["last_error"]})
        swhere, sargs = [], []
        if source:
            swhere.append("source=?")
            sargs.append(source)
        if reason:
            swhere.append("reason=?")
            sargs.append(reason)
        skips = [{**dict(r), "meta": json.loads(r["meta_json"] or "{}")} for r in db.all(
            "SELECT * FROM skips" + (" WHERE " + " AND ".join(swhere) if swhere else "")
            + " ORDER BY seen_at DESC LIMIT 300", sargs)]
        reasons = [r["reason"] for r in db.all("SELECT reason, count(*) n FROM skips GROUP BY reason ORDER BY n DESC")]
        return render(request, "activity.html", logs=logs, skips=skips, reasons=reasons, source=source,
                      reason=reason, show=show)

    @app.post("/activity/log-anyway")
    async def log_anyway(request: Request, token_: str = Form(alias="token"), key: str = Form(...)):
        check(request, token_)
        source, session_id, item_key = json.loads(key)
        ok = await bridge.approve(source, session_id, item_key)
        return back("/activity", show="skips", msg="logged" if ok else "no event data for that session")

    @app.get("/mappings", response_class=HTMLResponse)
    async def mappings(request: Request):
        db = bridge.db
        counts = {(r["source"], r["series_key"]): r["n"] for r in db.all(
            "SELECT source, series_key, count(*) n FROM logs GROUP BY source, series_key")}
        sugg: dict[tuple, list] = defaultdict(list)
        for r in db.all("SELECT * FROM suggestions ORDER BY score DESC"):
            sugg[(r["source"], r["series_key"])].append(dict(r))
        try:
            media = sorted(await bridge.km.list_media(), key=lambda m: m["title"])
        except Exception:
            media = []
        titles = {m["id"]: m["title"] for m in media}
        order = {"needs_mapping": 0, "pending_create": 1, "linked": 2, "ignored": 3}
        rows = []
        for r in db.all("SELECT * FROM series_map"):
            k = (r["source"], r["series_key"])
            rows.append({**dict(r), "logs": counts.get(k, 0), "entry": titles.get(r["media_id"]),
                         "suggestions": [s for s in sugg.get(k, []) if s["method"] != "merge"]})
        rows.sort(key=lambda r: (order.get(r["status"], 9), not r["suggestions"], r["source"], r["title"]))
        return render(request, "mappings.html", rows=rows, media=media)

    @app.post("/mappings")
    async def mappings_action(request: Request, token_: str = Form(alias="token"), source: str = Form(...),
                              series_key: str = Form(...), action: str = Form(...), media_id: str = Form(""),
                              title: str = Form("")):
        check(request, token_)
        if action == "link":
            if not media_id.isdigit():
                return back("/mappings", err="choose an entry first")
            err = save(settings.map_series(source, series_key, int(media_id), title))
            if not err:
                bridge.remap(source, series_key, int(media_id))
        elif action == "ignore":
            err = save(settings.ignore_series(source, series_key, title))
            if not err:
                bridge.remap(source, series_key, None, ignore=True)
        elif action == "new":
            err = save(settings.map_series(source, series_key, None))
            if not err:
                bridge.remap(source, series_key, None)
        else:
            err = f"unknown action {action}"
        return back("/mappings", err=err) if err else back("/mappings", msg=f"{title or series_key}: saved")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        libs = {}
        for name, src in bridge.sources.items():
            cached = lib_cache.get(name)
            if not cached or utcnow() - cached[0] > timedelta(minutes=10):
                try:
                    cached = (utcnow(), await src.list_libraries())
                except Exception:
                    cached = (utcnow(), [])
                lib_cache[name] = cached
            sc = bridge.cfg.source(name)
            libs[name] = []
            for lib in cached[1]:
                rule = sc.libraries.get(str(lib.id)) or sc.libraries.get(lib.name)
                eff = sc.library(lib.id, lib.name)
                key = lib.name if lib.name in sc.libraries or str(lib.id) not in sc.libraries else str(lib.id)
                if rule is None or rule.languages is _INHERIT:
                    langs = "inherit"
                elif rule.languages is None:
                    langs = "any"
                else:
                    langs = ",".join(rule.languages)
                libs[name].append({"id": lib.id, "name": lib.name, "type": lib.media_type, "key": key, "eff": eff,
                                   "explicit": rule is not None, "langs": langs,
                                   "unknown": (rule.unknown_language if rule and rule.unknown_language else "inherit")})
        return render(request, "settings.html", libs=libs, content_types=CONTENT_TYPES, activities=ACTIVITIES,
                      writable=store is not None,
                      youtube=youtube_status() if "youtube" in bridge.sources else None,
                      api_token=bridge.cfg.web_api_token)

    def parse_langs(value: str):
        value = (value or "").strip().lower()
        if value in ("", "inherit"):
            return "inherit"
        if value == "any":
            return None
        return [v.strip() for v in value.replace("、", ",").split(",") if v.strip()]

    @app.post("/settings/library")
    async def settings_library(request: Request, token_: str = Form(alias="token"), source: str = Form(...),
                               key: str = Form(...), track: str = Form(""), content_type: str = Form(...),
                               activity_type: str = Form(...), languages: str = Form("inherit"),
                               unknown_language: str = Form("inherit")):
        check(request, token_)
        err = save(settings.set_library(source, key, track=bool(track), content_type=content_type,
                                        activity_type=activity_type, languages=parse_langs(languages),
                                        unknown_language=unknown_language))
        return back("/settings", err=err) if err else back("/settings", msg=f"{source} · {key} saved")

    @app.post("/settings/source")
    async def settings_source(request: Request, token_: str = Form(alias="token"), source: str = Form(...)):
        check(request, token_)
        form = await request.form()
        mutators = []
        langs = parse_langs(str(form.get("languages", "")))
        mutators.append(settings.set_source(source, "languages", None if langs in (None, "inherit") else langs))
        for key in ("unknown_language", "played_flags", "generated_sessions", "audio_fallback", "default_track"):
            if key in form:
                value: Any = form[key]
                if key == "default_track":
                    value = value == "true"
                mutators.append(settings.set_source(source, key, value))
        err = save(*mutators)
        return back("/settings", err=err) if err else back("/settings", msg=f"{source} saved")

    @app.post("/settings/global")
    async def settings_global(request: Request, token_: str = Form(alias="token"),
                              min_session_minutes: float = Form(...), long_hours: float = Form(...),
                              long_action: str = Form(...)):
        check(request, token_)
        err = save(settings.set_top("min_session_minutes", min_session_minutes),
                   settings.set_top("long_sessions.hours", long_hours),
                   settings.set_top("long_sessions.action", long_action))
        return back("/settings", err=err) if err else back("/settings", msg="saved")

    # ------------------------------------------------------------- YouTube API
    @app.post("/api/youtube/progress")
    async def yt_progress(request: Request) -> dict:
        api_auth(request)
        body = await request.json()
        try:
            session_id, video_id = str(body["session_id"])[:80], str(body["video_id"])[:20]
            seconds = max(0.0, float(body["seconds"]))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "session_id, video_id and seconds are required")
        payload = {k: body.get(k) for k in ("title", "channel_id", "channel", "length", "channel_url", "account")}
        if body.get("account"):                 # the history reader checks the cookies against this account
            bridge.db.set_cursor("youtube", "tracked_account", str(body["account"])[:200])
        youtube().record_progress(session_id, video_id, seconds,
                                  float(body["position"]) if body.get("position") is not None else None, payload)
        bridge.db.set_cursor("youtube", "extension_seen", utcnow().isoformat())
        return {"ok": True}

    @app.post("/api/youtube/log")
    async def yt_log(request: Request) -> dict:
        api_auth(request)
        body = await request.json()
        minutes = body.get("minutes")
        try:
            minutes = float(minutes) if minutes not in (None, "", "0") else None
            res = await youtube().record_share(str(body.get("url", "")), minutes)
        except ValueError as e:
            raise HTTPException(400, str(e))
        res["message"] = f"Logged {res['minutes']} min · {res['title']} → {res['channel']}"
        return res

    @app.post("/api/youtube/cookies")
    async def yt_cookies(request: Request) -> dict:
        """The extension's sign-in refresh: a list of cookies (chrome.cookies format) or a cookies.txt body."""
        api_auth(request)
        ctype = request.headers.get("content-type", "")
        if "json" in ctype:
            body = await request.json()
            text = cookies_txt(body.get("cookies") or [])
            if body.get("session_index") is not None:   # which signed-in account the history reader should ask for
                bridge.db.set_cursor("youtube", "auth_user", str(int(body["session_index"])))
            if body.get("account"):
                bridge.db.set_cursor("youtube", "tracked_account", str(body["account"])[:200])
        else:
            text = (await request.body()).decode("utf-8", "replace")
        write_cookies(text, from_extension=True)
        if bridge.db.get_cursor("youtube", "history_state") != "ok":   # retry now rather than at the next 10-min slot
            bridge.db.set_cursor("youtube", "history_checked", None)
        return {"ok": True}

    @app.get("/api/youtube/status")
    async def yt_status(request: Request) -> dict:
        api_auth(request)
        return youtube_status()

    def youtube_status() -> dict:
        db = bridge.db
        return {"history": {k: db.get_cursor("youtube", f"history_{k}") for k in ("state", "checked", "ok", "error")},
                "extension_seen": db.get_cursor("youtube", "extension_seen"),
                "cookies": cookies_path().exists(),
                "cookies_shared": db.get_cursor("youtube", "cookies_shared"),
                "shares_today": shares_today(),
                "extension_latest": extension_latest()}

    def extension_zip(name: str) -> Path:
        """The image's own build (KB_EXTENSION_DIR), else a copy dropped into <data>/downloads."""
        built = os.environ.get("KB_EXTENSION_DIR")
        if built and (Path(built) / name).exists():
            return Path(built) / name
        return Path(bridge.cfg.db_path).parent / "downloads" / name

    def extension_latest() -> str | None:
        """Version of the extension zip on the Settings page, so the extension can show an update badge."""
        p = extension_zip("mochibridge-chrome.zip")
        try:
            with zipfile.ZipFile(p) as z:
                return json.loads(z.read("manifest.json")).get("version")
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return None

    def cookies_path() -> Path:
        sc = bridge.cfg.source("youtube")
        return Path(sc.extra.get("cookies_path") or Path(bridge.cfg.db_path).parent / "youtube-cookies.txt")

    def shares_today() -> int:
        day = datetime.now(ZoneInfo(bridge.cfg.timezone)).date().isoformat()
        counted = json.loads(bridge.db.get_cursor("youtube", "cookies_shares") or "{}")
        return int(counted.get("n", 0)) if counted.get("day") == day else 0

    def write_cookies(text: str, from_extension: bool = False) -> None:
        lines = [l for l in text.splitlines() if l.strip() and (l.startswith("#HttpOnly_") or not l.startswith("#"))]
        if not lines or not any("youtube.com" in l or "google.com" in l for l in lines):
            raise HTTPException(400, "that isn't a cookies.txt for youtube.com")
        p = cookies_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(p)
        if not from_extension:
            bridge.db.event("info", "youtube", "YouTube sign-in cookies uploaded")
            return
        # The extension re-shares whenever Google rotates the cookies (every few minutes while YouTube is open).
        # Those routine refreshes are counted for the Status page; only one that rescues a signed-out reader
        # (or the first share) goes to the activity feed.
        db = bridge.db
        first = db.get_cursor("youtube", "cookies_shared") is None
        db.set_cursor("youtube", "cookies_shared", iso(utcnow()))
        db.set_cursor("youtube", "cookies_shares", json.dumps({
            "day": datetime.now(ZoneInfo(bridge.cfg.timezone)).date().isoformat(), "n": shares_today() + 1}))
        state = db.get_cursor("youtube", "history_state")
        if state in (None, "ok"):
            db.set_cursor("youtube", "cookies_noted_state", None)
            if first:
                db.event("info", "youtube", "YouTube sign-in refreshed by the extension")
        elif db.get_cursor("youtube", "cookies_noted_state") != state:
            # Once per outage: while the reader is signed out / on the wrong account the extension keeps
            # re-sharing on every rotation, and the reader's own events already say when it recovers.
            db.set_cursor("youtube", "cookies_noted_state", state)
            db.event("info", "youtube", f"YouTube sign-in refreshed by the extension (history reader was {state})")

    @app.post("/youtube/quick-log")
    async def yt_quick_log(request: Request, token_: str = Form(alias="token"), url: str = Form(...),
                           minutes: str = Form("")):
        check(request, token_)
        try:
            res = await youtube().record_share(url, float(minutes) if minutes.strip() else None)
        except (ValueError, HTTPException) as e:
            return back("/", err=str(getattr(e, "detail", e)))
        return back("/", msg=f"Logged {res['minutes']} min · {res['title']} → {res['channel']}")

    @app.get("/download/{name}")
    async def download(name: str):
        """The browser extension zips built into the image."""
        if name not in ("mochibridge-chrome.zip", "mochibridge-firefox.zip"):
            raise HTTPException(404)
        p = extension_zip(name)
        if not p.exists():
            raise HTTPException(404, "not deployed yet")
        return FileResponse(p, media_type="application/zip", filename=name)

    @app.post("/settings/youtube-cookies")
    async def yt_cookie_upload(request: Request, token_: str = Form(alias="token"), file: UploadFile = File(...)):
        check(request, token_)
        try:
            write_cookies((await file.read()).decode("utf-8", "replace"))
        except HTTPException as e:
            return back("/settings", err=e.detail)
        return back("/settings", msg="YouTube cookies saved")

    app.state.youtube_status = youtube_status
    return app


def cookies_txt(cookies: list[dict]) -> str:
    """chrome.cookies.getAll() objects -> Netscape cookies.txt lines."""
    lines = []
    for c in cookies:
        domain = c.get("domain", "")
        if not domain:
            continue
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        expires = int(c.get("expirationDate") or 0)
        lines.append("\t".join([prefix + domain, include_sub, c.get("path", "/"),
                                "TRUE" if c.get("secure") else "FALSE", str(expires), c.get("name", ""),
                                c.get("value", "")]))
    return "\n".join(lines)
