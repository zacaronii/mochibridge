"""Library lists and the Jellyfin library map refresh without a restart; outages make one feed entry each."""
from datetime import datetime, timedelta, timezone

import respx

from kbridge.db import DB
from kbridge.service import Outage
from kbridge.sources import abs as absmod
from kbridge.sources import kavita as kv

from test_abs import CFG as ABS_CFG
from test_jellyfin import mock_jellyfin, source as jf_source
from test_kavita import CFG as KV_CFG

LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=2)


@respx.mock
async def test_kavita_lists_libraries_again_so_new_ones_are_read():
    libs = respx.get("http://kavita:5000/api/Library/libraries")
    libs.side_effect = [respx.MockResponse(json=[{"id": 3, "name": "漫画棚", "type": 0}]),
                        respx.MockResponse(json=[{"id": 3, "name": "漫画棚", "type": 0},
                                                 {"id": 5, "name": "新しい棚", "type": 0}])]
    history = respx.get("http://kavita:5000/api/Stats/reading-history").respond(json=[])
    src = kv.KavitaSource(KV_CFG, DB(":memory:"))
    await src.sessions()
    await src.sessions()                                   # fresh list: not fetched again
    assert libs.call_count == 1
    src._libraries_at = LONG_AGO
    await src.sessions()
    assert history.calls.last.request.url.params.get_list("Libraries") == ["3", "5"]
    await src.aclose()


@respx.mock
async def test_abs_relists_libraries_when_a_session_names_an_unknown_one(fixture):
    session = dict(fixture("abs/listening_sessions.json")["sessions"][0], libraryId="lib-new")
    libs = respx.get("http://abs/api/libraries")
    libs.side_effect = [respx.MockResponse(json={"libraries": [{"id": "lib-old", "name": "ポッドキャスト"}]}),
                        respx.MockResponse(json={"libraries": [{"id": "lib-old", "name": "ポッドキャスト"},
                                                               {"id": "lib-new", "name": "Audiobooks"}]})]
    respx.get("http://abs/api/me/listening-sessions").respond(json={"sessions": [session], "numPages": 1})
    src = absmod.ABSSource(ABS_CFG, DB(":memory:"))
    now = datetime.fromtimestamp(session["updatedAt"] / 1000, tz=timezone.utc)
    await src.list_libraries()
    src._libraries_at -= absmod.UNKNOWN_LIBRARY_RECHECK * 2
    (p,) = await src.poll(now, now)
    assert libs.call_count == 2
    assert p.info["library"] == "Audiobooks"           # named (and ruled on) by its real library
    await src.aclose()


@respx.mock
async def test_jellyfin_rescans_library_map_on_a_miss(tmp_path):
    mock_jellyfin([])
    src = jf_source(tmp_path, [])
    src.db.cache_set("jellyfin:scan", {"libmap": {}, "index": {}}, datetime.now(timezone.utc) + timedelta(hours=24))
    lib_id, name, rule = await src._library_rule("ser1")       # stale map without ser1: rescan finds it
    assert (lib_id, name, rule.track) == ("libA", "Anime", True)
    folders = next(r for r in respx.routes if "VirtualFolders" in repr(r.pattern))
    calls = folders.call_count
    await src._library_rule("not-a-series")                      # another miss right after: no second rescan
    assert folders.call_count == calls
    await src.aclose()


@respx.mock
async def test_jellyfin_rereads_an_incomplete_library_list(tmp_path):
    """Read while Jellyfin was starting, the list lacked Anime: its watches were skipped as "(unknown library)"."""
    mock_jellyfin([])
    src = jf_source(tmp_path, [])
    src.db.cache_set("jellyfin:scan", {"libmap": {"ser1": "libA"}, "index": {}},
                     datetime.now(timezone.utc) + timedelta(hours=24))
    src._libraries, src._libraries_at = {}, LONG_AGO
    lib_id, name, rule = await src._library_rule("ser1")
    assert (lib_id, name, rule.track) == ("libA", "Anime", True)
    await src.aclose()


def test_outage_makes_one_entry_when_it_starts_and_one_when_it_ends():
    db = DB(":memory:")
    o = Outage(db, "jellyfin", "poll")
    for _ in range(5):
        o.failed(ConnectionError("Name or service not known"))
    o.ok()
    o.ok()
    events = [(r["level"], r["message"]) for r in db.all("SELECT * FROM events ORDER BY id")]
    assert len(events) == 2
    assert events[0][0] == "error" and "poll failed" in events[0][1]
    assert events[1][0] == "info" and "working again" in events[1][1] and "5 failed attempts" in events[1][1]


@respx.mock
async def test_abs_picks_up_sessions_uploaded_late(fixture):
    """An app's backfill (or offline listening) arrives with its original dates, far behind the poll window."""
    base = fixture("abs/listening_sessions.json")["sessions"][0]
    t = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)
    ms = lambda dt: int(dt.timestamp() * 1000)
    old = dict(base, id="late-1", startedAt=ms(t - timedelta(days=10)), updatedAt=ms(t - timedelta(days=10)))
    known = dict(base, id="known-1", startedAt=ms(t - timedelta(days=10)), updatedAt=ms(t - timedelta(days=10)))
    before = dict(base, id="pre-1", startedAt=ms(t - timedelta(days=60)), updatedAt=ms(t - timedelta(days=60)))
    respx.get("http://abs/api/libraries").respond(json={"libraries": [{"id": base["libraryId"], "name": "ポッドキャスト"}]})
    listing = respx.get("http://abs/api/me/listening-sessions").respond(
        json={"sessions": [old, known, before], "numPages": 1})
    respx.get(url__regex=r"http://abs/api/items/.*").respond(json={"media": {"episodes": []}})
    db = DB(":memory:")
    db.execute("INSERT INTO skips(source, session_id, item_key, reason, seen_at) VALUES ('abs','known-1','x','too_short','')")
    src = absmod.ABSSource(ABS_CFG, db)

    assert await src.poll(t, t) == []                 # first scan only sets the floor: no old history
    db.set_cursor("abs", "late_since", (t - timedelta(days=30)).isoformat())
    assert await src.poll(t + timedelta(hours=1), t + timedelta(hours=1)) == []   # not due yet
    calls = listing.call_count
    ps = await src.poll(t + timedelta(hours=7), t + timedelta(hours=7))
    assert [p.session_id for p in ps] == ["late-1"]   # unseen and after the floor; known and too-old ones left out
    assert listing.call_count > calls
    await src.aclose()
