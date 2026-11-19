import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fakes import FakeKechimochi, entry
from kbridge import config
from kbridge.db import DB
from kbridge.service import Bridge, PollStats
from kbridge.sources import genkan
from kbridge.web.app import create_app

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def make(token="secret-token", media=None):
    cfg = config.parse({"mode": "live", "web": {"api_token": token},
                        "sources": {"genkan": {"enabled": True, "languages": ["ja"]}}}, {})
    db = DB(":memory:")
    km = FakeKechimochi(media or [])
    src = genkan.GenkanSource(cfg, db)
    return Bridge(cfg, db, km, {"genkan": src}), km, src


def body(seconds, characters, **kw):
    return {"session_id": "genkan-1-1795117258712", "kind": "vn", "title": "マルコと銀河竜", "platform": "genkan-pc",
            "started_at": T0.isoformat(), "seconds": seconds, "characters": characters, "final": False,
            "language": "ja", "quality": "measured", **kw}


async def poll(b, now):
    st = PollStats("genkan", now)
    for p in await b.sources["genkan"].poll(now - timedelta(seconds=15), now):
        await b.ingest(p, st)
    await b.sync()


async def test_growing_posts_become_one_log_with_minutes_and_characters():
    b, km, src = make()
    for i, (secs, chars) in enumerate(((60, 120), (300, 900), (900, 3400))):
        src.record_session(body(secs, chars), now=T0 + timedelta(minutes=5 * i))
        await poll(b, T0 + timedelta(minutes=5 * i))
    src.record_session(body(1200, 4100, final=True), now=T0 + timedelta(minutes=20))
    await poll(b, T0 + timedelta(minutes=20))
    (log,) = km.logs.values()
    assert log["duration_minutes"] == 20 and log["characters"] == 4100
    assert log["activity_type"] == "Reading"
    (media,) = km.media.values()
    assert media["title"] == "マルコと銀河竜" and media["content_type"] == "Visual Novel"


async def test_a_late_or_repeated_post_never_shrinks_a_session():
    b, km, src = make()
    src.record_session(body(900, 3000), now=T0)
    src.record_session(body(300, 1000), now=T0 + timedelta(minutes=1))     # an older heartbeat arriving late
    row = b.db.one("SELECT seconds, characters FROM genkan_sessions")
    assert (row["seconds"], row["characters"]) == (900, 3000)
    src.record_session(body(950, 3100, final=True), now=T0 + timedelta(minutes=2))
    src.record_session(body(960, 3200), now=T0 + timedelta(minutes=3))   # final stays final
    assert b.db.one("SELECT final FROM genkan_sessions")["final"] == 1


async def test_idle_sessions_become_final_and_games_log_as_playing():
    b, km, src = make()
    src.record_session(body(600, 0, kind="game", title="サバイバルキッズ2", romm_rom_id=117, platform="gbc"), now=T0)
    await poll(b, T0)
    await poll(b, T0 + timedelta(minutes=13))
    (log,) = km.logs.values()
    assert log["activity_type"] == "Playing" and log["characters"] == 0
    (media,) = km.media.values()
    assert media["content_type"] == "Videogame"
    assert json.loads(media["extra_data"])["romm"] == {"rom_id": 117}


async def test_a_rom_already_in_kechimochi_is_linked_by_its_romm_id():
    existing = entry(41, "Survival Kids 2", extra={"romm": {"rom_id": "117"}}, content_type="Videogame")
    b, km, src = make(media=[existing])
    src.record_session(body(600, 0, kind="game", title="サバイバルキッズ2", romm_rom_id=117, final=True), now=T0)
    await poll(b, T0)
    (log,) = km.logs.values()
    assert log["media_id"] == 41 and len(km.media) == 1


async def test_other_sources_still_log_no_characters():
    b, km, src = make()
    assert genkan.KINDS["vn"] == ("Visual Novel", "Reading")
    from kbridge.models import event_from_dict
    old = {"source": "youtube", "session_id": "yt:1", "item_key": "v", "note": "", "duration_seconds": 60,
           "final": True, "started_at": T0.isoformat(), "updated_at": T0.isoformat(),
           "series": {"source": "youtube", "series_key": "c", "title": "t", "library_id": "y",
                      "library_name": "Y", "content_type": "Youtube Video", "activity_type": "Watching"}}
    assert event_from_dict(old).characters == 0       # events stored before the field existed still load


def test_api_needs_the_token():
    b, km, src = make()
    c = TestClient(create_app(b, None))
    assert c.post("/api/genkan/session", json=body(60, 10)).status_code == 401
    auth = {"authorization": "Bearer secret-token"}
    assert c.post("/api/genkan/session", json=body(60, 10), headers=auth).json() == {"ok": True}
    assert c.post("/api/genkan/session", json={"title": "x"}, headers=auth).status_code == 400
    assert b.db.one("SELECT characters FROM genkan_sessions")["characters"] == 10


def test_api_disabled_without_token():
    b, km, src = make(token=None)
    c = TestClient(create_app(b, None))
    assert c.post("/api/genkan/session", json=body(60, 10), headers={"authorization": "Bearer x"}).status_code == 503


def test_bad_posts_get_400_with_the_reason():
    b, km, src = make()
    c = TestClient(create_app(b, None))
    auth = {"authorization": "Bearer secret-token"}
    for extra, says in (({"started_at": "yesterday"}, "started_at"), ({"romm_rom_id": "abc"}, "romm_rom_id"),
                        ({"language": 5}, "language"), ({"seconds": -1}, "seconds"), ({"seconds": None}, "seconds"),
                        ({"characters": 1.5}, "characters"), ({"final": True, "platform": ["x"]}, "platform")):
        r = c.post("/api/genkan/session", json={**body(60, 10), **extra}, headers=auth)
        assert r.status_code == 400 and says in r.json()["detail"], (extra, r.text)
    assert c.post("/api/genkan/session", json=[1, 2], headers=auth).status_code == 400
    assert c.post("/api/genkan/session", content=b"not json", headers=auth).status_code == 400
    assert b.db.one("SELECT count(*) n FROM genkan_sessions")["n"] == 0


def test_numbers_as_strings_and_times_without_an_offset_are_accepted():
    b, km, src = make()
    src.cfg.timezone = "America/Chicago"
    src.record_session(body("90", "300", romm_rom_id="117", started_at="2026-11-19T06:00:00"), now=T0)
    row = b.db.one("SELECT * FROM genkan_sessions")
    assert (row["seconds"], row["characters"], row["romm_rom_id"]) == (90.0, 300, 117)
    assert row["started_at"] == T0.isoformat()          # no offset: the bridge's time zone (06:00 Chicago = 12:00 UTC)
    src.record_session(body(90, 300, session_id="utc", started_at="2026-11-19T12:00:00Z"), now=T0)
    assert b.db.one("SELECT started_at FROM genkan_sessions WHERE session_id='utc'")["started_at"] == T0.isoformat()


async def test_a_session_that_went_idle_while_the_bridge_was_down_still_finishes():
    b, km, src = make()
    src.record_session(body(300, 900), now=T0)            # open session; the bridge sees it once
    await poll(b, T0 + timedelta(seconds=30))
    assert b.db.one("SELECT final FROM genkan_sessions")["final"] == 0
    # The bridge is down while the session goes idle; it comes back 20 minutes later.
    later = T0 + timedelta(minutes=20)
    st = PollStats("genkan", later)
    for p in await b.sources["genkan"].poll(later - timedelta(seconds=15), later):
        assert p.event.final
        await b.ingest(p, st)
    await b.sync()
    assert b.db.one("SELECT final FROM genkan_sessions")["final"] == 1
    assert [l["duration_minutes"] for l in km.logs.values()] == [5]
    assert await b.sources["genkan"].poll(later + timedelta(minutes=1), later + timedelta(minutes=2)) == []   # done
