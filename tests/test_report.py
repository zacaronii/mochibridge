import base64
from datetime import datetime, timezone

import pytest
import respx

from fakes import FakeKechimochi, entry
from kbridge import config, report
from kbridge.db import DB
from kbridge.filters import Decision
from kbridge.kechimochi import ReadOnlyKechimochi, ReadOnlyViolation
from kbridge.models import ActivityEvent, SeriesMeta
from kbridge.service import Bridge, PollStats
from kbridge.sources.base import Polled
from kbridge.sync import SyncStats


def marker(hid, instance="inst"):
    return f"[nagare:{instance}:{base64.urlsafe_b64encode(hid.encode()).decode().rstrip('=')}]\nSome title\n[/nagare]"


def test_marker_decoding():
    assert report.nagare_history_id(marker("rewatch:1:jellyfin|abc")) == "rewatch:1:jellyfin|abc"
    assert report.nagare_history_id("S1E03 · title") is None
    assert report.base_history_id("rewatch:2:jellyfin|abc") == "jellyfin|abc"


def cfg():
    return config.parse({"mode": "dry_run", "sources": {"jellyfin": {"enabled": True, "url": "x", "api_key": "k"}}}, {})


def polled(series_key, title, session, day, minutes, nagare_key=None, keep=True, reason=None, flags=()):
    m = SeriesMeta(source="jellyfin", series_key=series_key, title=title, library_id="L", library_name="Anime",
                   content_type="Anime", activity_type="Watching")
    t = datetime(2026, 10, day, 18, 0, tzinfo=timezone.utc)
    ev = ActivityEvent(source="jellyfin", session_id=session, item_key="i", series=m, note=f"S01E0{day % 9}",
                       started_at=t, updated_at=t, duration_seconds=minutes * 60, final=True, quality="position",
                       nagare_key=nagare_key, flags=list(flags))
    return Polled(session, "i", ev, Decision.ok() if keep else Decision.skip(reason))


async def test_report_flags_duplicates_and_compares_with_nagare(tmp_path):
    garo_manual = entry(7, "ガロの奇妙な旅", content_type="Anime")
    nagare_entry = entry(13, "Garo's Peculiar Journey (2012)", "Nagare · Jellyfin · Anime · Japanese",
                         {"nagare": {"history": [{"history_id": "jellyfin|ep5"}]}})
    km = FakeKechimochi([garo_manual, nagare_entry])
    km.logs = {
        3: {"id": 3, "media_id": 7, "duration_minutes": 24, "characters": 0, "date": "2026-10-18",
            "date_precision": "day", "activity_type": "Watching", "notes": ""},
        40: {"id": 40, "media_id": 13, "duration_minutes": 24, "characters": 0, "date": "2026-10-18",
             "date_precision": "day", "activity_type": "Watching", "notes": marker("jellyfin|ep5")},
        41: {"id": 41, "media_id": 13, "duration_minutes": 10, "characters": 0, "date": "2026-10-19",
             "date_precision": "day", "activity_type": "Watching", "notes": marker("rewatch:1:jellyfin|ep6")},
    }
    c = cfg()
    db = DB(":memory:")
    b = Bridge(c, db, km, {})
    st = PollStats("jellyfin", datetime(1970, 1, 1, tzinfo=timezone.utc))
    garo = polled("series:garo", "Garo's Peculiar Journey (2012)", "nagare:jellyfin|ep5", 18, 23.6, "jellyfin|ep5")
    garo.event.series.nagare_history_ids = ["jellyfin|ep5"]
    await b.ingest(garo, st)
    await b.ingest(polled("series:garo", "Garo's Peculiar Journey (2012)", "nagare:x", 20, 300, "jellyfin|ep7"), st)
    await b.ingest(polled("series:garo", "Garo's Peculiar Journey (2012)", "jfplayed:old", 9, 24, keep=False,
                          reason="played_flag_only", flags=["estimate"]), st)
    rep = await report.build(db, km, c, {"jellyfin": st}, SyncStats())

    inc = [r for r in rep.rows if r["included"] == "yes"]
    assert {r["target"] for r in inc} == {"#13 Garo's Peculiar Journey (2012)"}
    assert any("long_session>4h" in r["flags"] for r in inc)
    skipped = [r for r in rep.rows if r["included"] == "no"]
    assert skipped[0]["reason"] == "played_flag_only" and skipped[0]["flags"] == "estimate"

    comp = {(c_["series"], c_["date"]): c_ for c_ in rep.comparison}
    same_day = comp[("Garo's Peculiar Journey (2012)", "2026-10-18")]
    assert (same_day["nagare_minutes"], same_day["bridge_minutes"], same_day["note"]) == (24, 24, "")
    assert comp[("(unmatched) Garo's Peculiar Journey (2012)", "2026-10-19")]["note"] == "only Nagare"
    assert rep.kechimochi_counts == {"media": 2, "logs": 3, "nagare_logs": 2, "manual_logs": 1}

    report.write(rep, tmp_path)
    html = (tmp_path / "backfill_report.html").read_text()
    assert "<title>Backfill Preview</title>" in html and "only Nagare" in html
    assert (tmp_path / "backfill_preview.csv").read_text().startswith("source,library,series,target")


async def test_duplicate_flag_via_suggested_entry():
    km = FakeKechimochi([entry(24, "ルミナリア", content_type="Manga")])
    km.logs = {45: {"id": 45, "media_id": 24, "duration_minutes": 35, "characters": 0, "date": "2026-10-23",
                    "date_precision": "day", "activity_type": "Reading", "notes": ""}}
    c = cfg()
    db = DB(":memory:")
    b = Bridge(c, db, km, {})
    st = PollStats("jellyfin", datetime(1970, 1, 1, tzinfo=timezone.utc))
    await b.ingest(polled("kv:lumi", "黄昏のルミナリア", "kv:1", 23, 12), st)
    rep = await report.build(db, km, c, {"jellyfin": st}, SyncStats())
    (row,) = [r for r in rep.rows if r["included"] == "yes"]
    assert row["target"] == "new" and "#24 ルミナリア (contains)" in row["suggestion"]
    assert "possible_duplicate:#45 35m (suggested entry)" in row["flags"]


@respx.mock
async def test_read_only_client_refuses_writes():
    respx.get("http://k/api/media").respond(json=[{"id": 1, "title": "x", "description": ""}])
    km = ReadOnlyKechimochi("http://k")
    assert len(await km.list_media()) == 1
    with pytest.raises(ReadOnlyViolation):
        await km.create_log({"media_id": 1, "duration_minutes": 5})
    with pytest.raises(ReadOnlyViolation):
        await km.update_media(1, lambda m: {**m, "description": "new"})   # GET allowed, PUT refused
    await km.aclose()
