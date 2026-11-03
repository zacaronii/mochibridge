"""Nagare -> bridge migration.

    migrate --check            preflight (kechimochi version, Nagare sync off, counts)
    migrate --plan             read-only: what would happen, written to <out>/migration_plan.{json,md}
    migrate --apply            backups, Nagare cut-over, then steps a-e, journaled and resumable

Steps, applied in the order a, b, c, e, d (each operation re-checks its preconditions right before it runs):
    a  delete logs carrying Nagare's `[nagare:…]` marker (never anything else)
    b  delete the manual logs listed in `migration.delete_logs` (confirmed by you)
    c  backfill: full history through the normal pipeline, synced live
    d  clean surviving Nagare entries: drop `nagare` extra_data, clear the `Nagare · …` variant, correct
       content type / activity / language only where they still hold Nagare's original values
    e  delete Nagare entries left with no logs and no milestones and not used by the bridge
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import extra, idmap
from .config import Config
from .db import DB, iso, utcnow
from .kechimochi import Kechimochi, KechimochiError, media_payload
from .report import nagare_history_id
from .resolver import _nagare_ids
from .service import Bridge, PollStats

log = logging.getLogger(__name__)
NAGARE_VARIANT = re.compile(r"^Nagare · ")
EXPECTED_VERSION = "web-0.3.3"


class MigrationError(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers
def nagare_blob(media: dict[str, Any]) -> dict[str, Any] | None:
    data = extra.parse(media.get("extra_data"))
    for k, v in data.items():
        if k.lower() == "nagare":
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    return {}
            return v if isinstance(v, dict) else {}
    return None


def nagare_group(media: dict[str, Any]) -> dict[str, str]:
    """Nagare's original values for the entry, from its group_key [server, title, activity, content_type, language]."""
    blob = nagare_blob(media) or {}
    try:
        key = json.loads(blob.get("group_key") or "[]")
    except (TypeError, ValueError):
        key = []
    names = ("server", "title", "activity", "content_type", "language")
    return {n: key[i] for i, n in enumerate(names) if i < len(key) and isinstance(key[i], str)}


def is_nagare_log(log_: dict[str, Any]) -> bool:
    return nagare_history_id(log_.get("notes")) is not None


def cleaned_entry(media: dict[str, Any], desired: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """The Nagare-made entry after step d, and a human-readable list of changes."""
    out = dict(media)
    changes = []
    data = extra.parse(media.get("extra_data"))
    stripped = {k: v for k, v in data.items() if k.lower() != "nagare"}
    if stripped != data:
        out["extra_data"] = json.dumps(stripped, ensure_ascii=False)
        changes.append("remove Nagare sync data from extra_data")
    if NAGARE_VARIANT.match(media.get("variant") or ""):
        out["variant"] = ""
        changes.append(f"clear variant {media['variant']!r}")
    if desired:
        group = nagare_group(media)
        for field, gkey in (("content_type", "content_type"), ("default_activity_type", "activity"),
                            ("language", "language")):
            want = desired.get(field)
            if want and media.get(field) == group.get(gkey) and media.get(field) != want:
                out[field] = want
                changes.append(f"{field}: {media.get(field)!r} → {want!r} (Nagare's value)")
    return out, changes


@dataclasses.dataclass
class Migration:
    cfg: Config
    db: DB
    km: Kechimochi
    bridge: Bridge
    nagare_url: str = "http://nagare:9470"

    # -------------------------------------------------------------- journal
    def done(self, step: str, target: str) -> bool:
        return self.db.one("SELECT 1 FROM migration_journal WHERE step=? AND target=? AND result='done'",
                           (step, target)) is not None

    def journal(self, step: str, target: str, before: Any, result: str) -> None:
        self.db.execute("INSERT INTO migration_journal(ts, step, target, before_json, result) VALUES (?,?,?,?,?)",
                        (iso(utcnow()), step, target, json.dumps(before, ensure_ascii=False), result))

    # -------------------------------------------------------------- preflight
    async def check(self, require_nagare_off: bool = True) -> dict[str, Any]:
        version = await self.km.version()
        if version != EXPECTED_VERSION:
            raise MigrationError(f"kechimochi is {version}; the migration was verified against {EXPECTED_VERSION}")
        media, logs = await self.km.list_media(), await self.km.list_logs()
        nagare = None
        if require_nagare_off:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    nagare = (await c.get(f"{self.nagare_url}/api/kechimochi/status")).json()
            except (httpx.HTTPError, ValueError) as e:
                raise MigrationError(f"can't confirm Nagare's kechimochi sync is off: {e!r}") from e
            if nagare.get("enabled") or nagare.get("running"):
                raise MigrationError("Nagare's kechimochi sync is enabled or running; turn it off first")
        return {"kechimochi": version, "media": len(media), "logs": len(logs),
                "nagare_logs": sum(1 for l in logs if is_nagare_log(l)),
                "nagare_media": sum(1 for m in media if nagare_blob(m) is not None),
                "nagare_sync": "off" if nagare is not None else "not checked"}

    def golive(self) -> str:
        """Nagare covers Jellyfin until live sampling began (first live poll), so nothing is counted twice."""
        existing = self.db.get_cursor("jellyfin", "nagare_until")
        if existing:
            return existing
        row = self.db.one("SELECT min(ts) ts FROM events WHERE source='jellyfin' AND message LIKE 'first poll%'")
        value = (row["ts"] if row and row["ts"] else None) or iso(utcnow())
        self.db.set_cursor("jellyfin", "nagare_until", value)
        self.db.event("info", "jellyfin", f"Nagare covers Jellyfin until {value}; live tracking after")
        return value

    # -------------------------------------------------------------- plan
    async def plan(self, linked_media: set[int]) -> dict[str, Any]:
        """What steps a, b, d and e would do now. `linked_media`: entries the bridge maps series to (from a
        backfill preview or the live DB). Step c is described by the backfill preview itself."""
        media = {m["id"]: m for m in await self.km.list_media()}
        logs = await self.km.list_logs()
        by_media: dict[int, list[dict]] = defaultdict(list)
        for l in logs:
            by_media[l["media_id"]].append(l)
        nagare_logs = [l for l in logs if is_nagare_log(l)]
        manual = []
        for lid in self.cfg.migration_delete_logs:
            l = next((x for x in logs if x["id"] == lid), None)
            manual.append({"id": lid, "found": l is not None, "nagare": bool(l and is_nagare_log(l)),
                           "log": l, "title": media.get(l["media_id"], {}).get("title") if l else None})
        desired_by_media = self._desired_by_media()
        cleanup, deletes = [], []
        for mid, m in sorted(media.items()):
            if nagare_blob(m) is None:
                continue
            remaining = [l for l in by_media.get(mid, []) if not is_nagare_log(l)
                         and l["id"] not in self.cfg.migration_delete_logs]
            if mid in linked_media:
                after, changes = cleaned_entry(m, desired_by_media.get(mid))
                cleanup.append({"id": mid, "title": m["title"], "changes": changes})
            elif not remaining:
                deletes.append({"id": mid, "title": m["title"], "variant": m.get("variant"),
                                "nagare_logs": len(by_media.get(mid, [])), "uid": m.get("uid")})
            else:
                cleanup.append({"id": mid, "title": m["title"], "changes": cleaned_entry(m, None)[1],
                                "note": f"kept: {len(remaining)} non-Nagare logs"})
        return {"nagare_logs": [{"id": l["id"], "media_id": l["media_id"], "date": l["date"],
                                 "minutes": l["duration_minutes"], "history_id": nagare_history_id(l["notes"])}
                                for l in nagare_logs],
                "manual_deletes": manual, "cleanup": cleanup, "media_deletes": deletes,
                "before": {"media": len(media), "logs": len(logs)}}

    def _desired_by_media(self) -> dict[int, dict[str, Any]]:
        out = {}
        for r in self.db.all("SELECT media_id, desired_json FROM series_map WHERE media_id IS NOT NULL"):
            out.setdefault(r["media_id"], json.loads(r["desired_json"]))
        return out

    # -------------------------------------------------------------- apply
    async def step_a(self) -> int:
        n = 0
        for l in await self.km.list_logs():
            target = f"log:{l['id']}"
            if not is_nagare_log(l) or self.done("a", target):
                continue
            await self.km.delete_log(l["id"])
            self.journal("a", target, l, "done")
            n += 1
        return n

    async def step_b(self) -> int:
        n = 0
        logs = {l["id"]: l for l in await self.km.list_logs()}
        for lid in self.cfg.migration_delete_logs:
            target = f"log:{lid}"
            if self.done("b", target):
                continue
            l = logs.get(lid)
            if l is None:
                self.journal("b", target, None, "missing")
                continue
            await self.km.delete_log(lid)
            self.journal("b", target, l, "done")
            n += 1
        return n

    async def step_c(self, sources: dict[str, Any], nagare_db: str | None) -> dict[str, Any]:
        """Backfill through the live pipeline: same filters/resolver/desired logs, then a live sync."""
        idmap.refresh(self.db, self.cfg.idmap_db, self.cfg.idmap_refresh_hours)
        now = utcnow()
        stats = {}
        for name, src in sources.items():
            polled = await (src.history(now, nagare_db=nagare_db) if name == "jellyfin" else src.history(now))
            st = PollStats(name, datetime(1970, 1, 1, tzinfo=timezone.utc))
            st.sessions = len(polled)
            self.bridge.resolver.invalidate()
            for p in polled:
                await self.bridge.ingest(p, st)
            stats[name] = {"sessions": st.sessions, "logged": st.logged, "skipped": st.skipped}
        sync = await self.bridge.syncer.run_once()
        if sync.errors:
            raise MigrationError(f"backfill sync had {sync.errors} errors; see `pending` (resume with --apply)")
        self.journal("c", "backfill", stats, "done")
        return {"sources": stats, "media_created": sync.media_created, "logs_created": sync.logs_created,
                "covers": sync.covers, "media_updated": sync.media_updated}

    async def step_d(self) -> list[dict[str, Any]]:
        linked = self._desired_by_media()
        out = []
        for m in await self.km.list_media():
            if nagare_blob(m) is None or self.done("d", f"media:{m['id']}"):
                continue
            after, changes = cleaned_entry(m, linked.get(m["id"]))
            if not changes:
                continue
            try:
                await self.km.put_media(after)
            except KechimochiError as e:
                if e.status != 409 or "variant" not in " ".join(changes):
                    raise
                after["variant"] = m["variant"]          # another entry already has this title without variant
                changes = [c for c in changes if not c.startswith("clear variant")] + ["kept variant (409)"]
                await self.km.put_media(after)
            self.journal("d", f"media:{m['id']}", m, "done")
            out.append({"id": m["id"], "title": m["title"], "changes": changes})
        return out

    async def step_e(self) -> list[dict[str, Any]]:
        """Runs before d, so Nagare-made entries are still recognisable by their `nagare` extra_data."""
        used = {r["media_id"] for r in self.db.all("SELECT media_id FROM series_map WHERE media_id IS NOT NULL")}
        out = []
        for m in await self.km.list_media():
            if nagare_blob(m) is None or m["id"] in used or self.done("e", f"media:{m['id']}"):
                continue
            if await self.km.logs_for_media(m["id"]):
                continue
            if m.get("uid") and await self.km.milestones(m["uid"]):
                continue
            await self.km.delete_media(m["id"])
            self.journal("e", f"media:{m['id']}", m, "done")
            out.append({"id": m["id"], "title": m["title"]})
        return out

    async def verify(self) -> dict[str, Any]:
        media, logs = await self.km.list_media(), await self.km.list_logs()
        per_series = Counter()
        minutes = Counter()
        for l in logs:
            per_series[l["title"]] += 1
            minutes[l["title"]] += l["duration_minutes"]
        return {"media": len(media), "logs": len(logs),
                "nagare_logs": sum(1 for l in logs if is_nagare_log(l)),
                "nagare_media": [m["id"] for m in media if nagare_blob(m) is not None],
                "pending_logs": self.db.one("SELECT count(*) n FROM logs WHERE synced_hash IS NOT desired_json")["n"],
                "per_series": {t: {"logs": per_series[t], "minutes": minutes[t]} for t in sorted(per_series)}}


def write_markdown(path: Path, title: str, sections: dict[str, Any]) -> None:
    lines = [f"# {title}", "", f"Generated {iso(utcnow())}", ""]
    for name, value in sections.items():
        lines += [f"## {name}", "", "```json", json.dumps(value, ensure_ascii=False, indent=1), "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
