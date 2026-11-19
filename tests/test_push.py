import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fakes import FakeKechimochi, entry
from kbridge import config
from kbridge.db import DB
from kbridge.service import Bridge, PollStats
from kbridge.sources import push
from kbridge.web.app import create_app

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
AUTH = {"authorization": "Bearer secret-token"}


def make(token="secret-token", media=None, libraries=None):
    cfg = config.parse({"mode": "live", "web": {"api_token": token},
                        "sources": {"push": {"enabled": True, "languages": ["ja"], "libraries": libraries or {}}}}, {})
    db = DB(":memory:")
    km = FakeKechimochi(media or [])
    src = push.PushSource(cfg, db)
    return Bridge(cfg, db, km, {"push": src}), km, src


def body(seconds, characters, entry_=None, **kw):
    return {"client": "genkan", "session_id": "genkan-1-1795117258712", "started_at": T0.isoformat(),
            "seconds": seconds, "characters": characters, "final": False, "content_type": "Visual Novel",
            "activity_type": "Reading", "language": "ja",
            "entry": entry_ or {"title": "マルコと銀河竜", "fields": {"Platform": "PC"}}, **kw}


GAME = {"title": "サバイバルキッズ2", "ids": {"romm.rom_id": 117}, "fields": {"Platform": "gbc"}}


async def poll(b, now):
    st = PollStats("push", now)
    for p in await b.sources["push"].poll(now - timedelta(seconds=15), now):
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
    assert log["duration_minutes"] == 20 and log["characters"] == 4100 and log["activity_type"] == "Reading"
    (media,) = km.media.values()
    assert media["title"] == "マルコと銀河竜" and media["content_type"] == "Visual Novel"
    assert json.loads(media["extra_data"])["Platform"] == "PC"


async def test_a_late_or_repeated_post_never_shrinks_a_session():
    b, km, src = make()
    src.record_session(body(900, 3000, final=True), now=T0)
    src.record_session(body(300, 100), now=T0 + timedelta(seconds=5))      # stale retry arrives late
    row = b.db.one("SELECT * FROM push_sessions")
    assert (row["seconds"], row["characters"], row["final"]) == (900, 3000, 1)


async def test_idle_sessions_become_final_and_ids_go_into_extra_data():
    b, km, src = make()
    src.record_session(body(600, 0, GAME, content_type="Videogame", activity_type="Playing"), now=T0)
    await poll(b, T0)
    await poll(b, T0 + timedelta(minutes=13))
    (log,) = km.logs.values()
    assert log["activity_type"] == "Playing" and log["characters"] == 0
    (media,) = km.media.values()
    assert media["content_type"] == "Videogame"
    extra = json.loads(media["extra_data"])
    assert extra["romm"] == {"rom_id": "117"} and extra["Platform"] == "gbc"


async def test_an_entry_already_in_kechimochi_is_linked_by_its_id():
    existing = entry(41, "Survival Kids 2", extra={"romm": {"rom_id": "117"}}, content_type="Videogame")
    b, km, src = make(media=[existing])
    src.record_session(body(600, 0, GAME, content_type="Videogame", activity_type="Playing", final=True), now=T0)
    await poll(b, T0)
    (log,) = km.logs.values()
    assert log["media_id"] == 41 and len(km.media) == 1


async def test_one_entry_per_id_even_if_the_title_changes():
    b, km, src = make()
    src.record_session(body(600, 0, GAME, final=True), now=T0)
    src.record_session(body(600, 0, {**GAME, "title": "Survival Kids 2"}, session_id="s2", final=True), now=T0)
    await poll(b, T0)
    assert len(km.media) == 1 and len(km.logs) == 2


async def test_types_default_to_the_clients_rules_and_untracked_clients_are_skipped():
    b, km, src = make(libraries={"reader": {"content_type": "Novel", "activity_type": "Reading"},
                                 "noisy": {"track": False}})
    src.record_session({"client": "reader", "session_id": "r1", "started_at": T0.isoformat(), "seconds": 600,
                        "final": True, "entry": {"title": "本"}}, now=T0)
    src.record_session({**body(600, 0, final=True), "client": "noisy", "session_id": "n1"}, now=T0)
    await poll(b, T0)
    (media,) = km.media.values()
    assert (media["title"], media["content_type"]) == ("本", "Novel")
    assert b.db.one("SELECT reason FROM skips WHERE source='push'")["reason"] == "library_untracked"
    assert [l.name for l in await src.list_libraries()] == ["noisy", "reader"]    # shown on the Settings page


async def test_two_apps_can_use_the_same_session_id():
    b, km, src = make()
    src.record_session(body(600, 0, final=True), now=T0)
    src.record_session({**body(300, 0, final=True), "client": "other"}, now=T0)
    assert b.db.one("SELECT count(*) n FROM push_sessions")["n"] == 2


async def test_other_sources_still_log_no_characters():
    from kbridge.models import event_from_dict
    old = {"source": "youtube", "session_id": "yt:1", "item_key": "v", "note": "", "duration_seconds": 60,
           "final": True, "started_at": T0.isoformat(), "updated_at": T0.isoformat(),
           "series": {"source": "youtube", "series_key": "c", "title": "t", "library_id": "y",
                      "library_name": "Y", "content_type": "Youtube Video", "activity_type": "Watching"}}
    assert event_from_dict(old).characters == 0       # events stored before the field existed still load


async def test_a_session_that_went_idle_while_the_bridge_was_down_still_finishes():
    b, km, src = make()
    src.record_session(body(300, 900), now=T0)            # open session; the bridge sees it once
    await poll(b, T0 + timedelta(seconds=30))
    assert b.db.one("SELECT final FROM push_sessions")["final"] == 0
    later = T0 + timedelta(minutes=20)                    # down while it went idle, back 20 minutes later
    st = PollStats("push", later)
    for p in await b.sources["push"].poll(later - timedelta(seconds=15), later):
        assert p.event.final
        await b.ingest(p, st)
    await b.sync()
    assert b.db.one("SELECT final FROM push_sessions")["final"] == 1
    assert [l["duration_minutes"] for l in km.logs.values()] == [5]
    assert await b.sources["push"].poll(later + timedelta(minutes=1), later + timedelta(minutes=2)) == []


def test_numbers_as_strings_and_times_without_an_offset_are_accepted():
    b, km, src = make()
    src.cfg.timezone = "America/Chicago"
    src.record_session(body("90", "300", started_at="2026-11-19T06:00:00"), now=T0)
    row = b.db.one("SELECT * FROM push_sessions")
    assert (row["seconds"], row["characters"]) == (90.0, 300)
    assert row["started_at"] == T0.isoformat()          # no offset: the bridge's time zone (06:00 Chicago = 12:00 UTC)


def test_api_needs_the_token_and_the_source():
    b, km, src = make()
    c = TestClient(create_app(b, None))
    assert c.post("/api/sessions", json=body(60, 10)).status_code == 401
    assert c.post("/api/sessions", json=body(60, 10), headers=AUTH).json() == {"ok": True}
    assert b.db.one("SELECT characters FROM push_sessions")["characters"] == 10
    b2, _, _ = make(token=None)
    assert TestClient(create_app(b2, None)).post("/api/sessions", json=body(60, 10), headers=AUTH).status_code == 503


def test_bad_posts_get_400_with_the_reason():
    b, km, src = make()
    c = TestClient(create_app(b, None))
    for change, says in (({"client": "Genkan Launcher"}, "client"), ({"client": None}, "client"),
                         ({"started_at": "yesterday"}, "started_at"), ({"seconds": -1}, "seconds"),
                         ({"seconds": None}, "seconds"), ({"characters": 1.5}, "characters"),
                         ({"content_type": "Game"}, "content_type"), ({"activity_type": "Gaming"}, "activity_type"),
                         ({"language": 5}, "language"), ({"entry": None}, "entry"), ({"entry": {"ids": {}}}, "title"),
                         ({"entry": {"title": "x", "ids": {"romm rom": 1}}}, "entry.ids"),
                         ({"entry": {"title": "x", "ids": {"romm.rom_id": True}}}, "entry.ids"),
                         ({"entry": {"title": "x", "fields": {"Platform": ["pc"]}}}, "entry.fields")):
        r = c.post("/api/sessions", json={**body(60, 10), **change}, headers=AUTH)
        assert r.status_code == 400 and says in r.json()["detail"], (change, r.text)
    assert c.post("/api/sessions", json=[1, 2], headers=AUTH).status_code == 400
    assert c.post("/api/sessions", content=b"not json", headers=AUTH).status_code == 400
    assert b.db.one("SELECT count(*) n FROM push_sessions")["n"] == 0
