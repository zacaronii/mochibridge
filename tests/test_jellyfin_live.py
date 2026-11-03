import json
from datetime import datetime, timedelta, timezone

import respx

from kbridge import config
from kbridge.db import DB
from kbridge.sources import jellyfin as jf
from test_jellyfin import JF, RAW, SERIES, UID, episode, mock_jellyfin

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
EP = {**episode("b71eb", 2, 5, "Rainy Season"), "MediaStreams": [
    {"Type": "Audio", "Index": 3, "Language": "jpn", "IsDefault": True},
    {"Type": "Audio", "Index": 4, "Language": "eng", "IsDefault": False}]}


def src():
    db = DB(":memory:")
    db.execute("INSERT INTO idmap VALUES ('anidb','91234','R','anilist','912345',NULL,'1-12','1-12')")
    return jf.JellyfinSource(config.parse(RAW, {}), db)


def session(pos, paused=False, audio=3, item=EP, user=UID):
    return {"Id": "dev-session-1", "UserId": user, "Client": "ExampleClient",
            "NowPlayingItem": {"Id": item["Id"], "Name": item["Name"], "Type": "Episode"},
            "PlayState": {"PositionTicks": int(pos * 10_000_000), "IsPaused": paused, "AudioStreamIndex": audio}}


def script():
    """0-120 s playing Japanese, 120-180 s paused but NOT reported (position frozen), 180-210 s Japanese,
    210-240 s English; then nothing playing. Expected: 150 s counted, all Japanese."""
    steps = []
    for t in range(0, 241, 15):
        if t <= 120:
            pos = t
        elif t < 180:
            pos = 120
        else:
            pos = 120 + (t - 180)
        steps.append((t, [session(pos, audio=3 if t < 210 else 4)]))
    return steps


@respx.mock
async def test_live_play_measured_from_samples():
    mock_jellyfin([EP])
    route = respx.get(f"{JF}/Sessions")
    s = src()
    since, events = T0, {}
    for t, sessions in script() + [(255, []), (600, [])]:
        route.respond(json=sessions + [session(999, user="someone-else")])
        now = T0 + timedelta(seconds=t)
        for p in await s.poll(since, now):
            events[p.session_id] = p
        since = now
    (p,) = events.values()
    ev = p.event
    assert p.session_id == f"jf:play:dev-session-1:b71eb:{int(T0.timestamp())}"
    assert ev.duration_seconds == 150 and ev.language == "ja"          # unreported pause and English excluded
    assert ev.note == "S02E05 · Rainy Season" and ev.series.series_key == "series:ser1"
    assert ev.quality == "measured" and ev.final and p.decision.keep
    assert s.db.one("SELECT count(*) n FROM jf_samples WHERE session_key='dev-session-1'")["n"] == 17
    await s.aclose()


@respx.mock
async def test_english_only_play_is_skipped_with_reason():
    mock_jellyfin([EP])
    route = respx.get(f"{JF}/Sessions")
    s = src()
    since, last = T0, None
    for t in range(0, 301, 15):
        route.respond(json=[session(t, audio=4)] if t <= 180 else [])
        now = T0 + timedelta(seconds=t)
        last = (await s.poll(since, now)) or last
        since = now
    assert last[0].decision.reason == "language:en"
    await s.aclose()


@respx.mock
async def test_plugin_backstop_matches_and_fills_gaps():
    mock_jellyfin([EP, episode("ep4", 2, 4, "First Snow")])
    respx.get(f"{JF}/Sessions").respond(json=[])
    plugin = respx.post(f"{JF}/user_usage_stats/submit_custom_query")
    s = src()
    # a sampled play of b71eb around 11:00
    for i in range(5):
        ts = T0 - timedelta(hours=1) + timedelta(seconds=15 * i)
        s.db.execute("INSERT INTO jf_samples VALUES ('dev', 'b71eb', ?, ?, 0, 3, 'ja')", (ts.isoformat(), i * 15 * 10_000_000))
    plugin.respond(json={"colums": ["rowid", "DateCreated", "ItemId", "ItemType", "ItemName", "ClientName", "PlayDuration"],
                         "results": [["5", (T0 - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.0000000"), "b71eb",
                                      "Episode", "x", "ExampleClient", "60"],
                                     ["6", (T0 - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S.0000000"), "ep4",
                                      "Episode", "y", "ExampleClient", "99999"]], "message": ""})
    s.db.set_cursor("jellyfin", "plugin_rowid", "0")              # as if live tracking started before these rows
    ps = await s.poll(T0, T0)
    sql = json.loads(plugin.calls.last.request.content)["CustomQueryString"]
    assert f"UserId='{UID.replace('-', '')}'" in sql and "rowid > 0" in sql
    assert [p.session_id for p in ps] == ["jf:pr:6"]                     # row 5 was measured by the sampler
    ev = ps[0].event
    assert ev.quality == "plugin" and "unsampled" in ev.flags
    assert ev.duration_seconds == 1447.04                                # capped at the episode runtime
    assert s.db.get_cursor("jellyfin", "plugin_rowid") == "6"
    # checked again only after plugin_poll_seconds
    before = plugin.call_count
    await s.poll(T0 + timedelta(seconds=15), T0 + timedelta(seconds=15))
    assert plugin.call_count == before
    await s.aclose()


def test_nagare_until_prefers_db_cursor():
    s = src()
    assert s.nagare_until() is None
    s.db.set_cursor("jellyfin", "nagare_until", "2026-11-02T00:00:00+00:00")
    assert s.nagare_until() == datetime(2026, 11, 2, tzinfo=timezone.utc)



@respx.mock
async def test_first_plugin_check_skips_rows_from_before_live_tracking():
    mock_jellyfin([EP])
    respx.get(f"{JF}/Sessions").respond(json=[])
    respx.post(f"{JF}/user_usage_stats/submit_custom_query").respond(json={
        "colums": ["rowid", "DateCreated", "ItemId", "ItemType", "ItemName", "ClientName", "PlayDuration"],
        "results": [["1", "2026-11-01 17:00:00.0000000", "b71eb", "Episode", "x", "ExampleClient", "31"],
                    ["3", "2026-11-01 19:00:00.0000000", "b71eb", "Episode", "x", "ExampleClient", "358"]], "message": ""})
    s = src()
    assert await s.poll(T0, T0) == []
    assert s.db.get_cursor("jellyfin", "plugin_rowid") == "3"
    # rows before the Nagare cut-over are never logged from the plugin either
    s.db.set_cursor("jellyfin", "plugin_rowid", "0")
    s.db.set_cursor("jellyfin", "plugin_checked_at", None)
    s.db.set_cursor("jellyfin", "nagare_until", "2026-11-02T00:00:00+00:00")
    assert await s.poll(T0, T0) == []
    await s.aclose()


@respx.mock
async def test_played_flag_detection_skips_real_plays():
    real = {**EP, "UserData": {"Played": True, "LastPlayedDate": "2026-11-02T12:20:00.0000000Z"}}
    marked = {**episode("ep4", 2, 4, "First Snow"),
              "UserData": {"Played": True, "LastPlayedDate": "2026-11-02T12:30:00.0000000Z"}}
    old = {**episode("ep3", 2, 3, "Old"), "UserData": {"Played": True, "LastPlayedDate": "2026-10-01T12:00:00.0000000Z"}}
    mock_jellyfin([EP, marked], played=[marked, real, old])
    respx.get(f"{JF}/Sessions").respond(json=[])
    respx.post(f"{JF}/user_usage_stats/submit_custom_query").respond(
        json={"colums": ["rowid", "DateCreated", "ItemId", "PlayDuration"], "results": [], "message": ""})
    RAW["sources"]["jellyfin"]["played_flags"] = "review"
    try:
        s = src()
    finally:
        RAW["sources"]["jellyfin"].pop("played_flags")
    s.db.set_cursor("jellyfin", "played_cursor", "2026-11-02T00:00:00+00:00")
    s.db.set_cursor("jellyfin", "plugin_rowid", "0")
    for i in range(5):   # a measured play of b71eb just before it was marked played
        ts = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=4 * i)
        s.db.execute("INSERT INTO jf_samples VALUES ('dev', 'b71eb', ?, ?, 0, 3, 'ja')", (ts.isoformat(), i * 240 * 10_000_000))
    now = datetime(2026, 11, 2, 12, 40, tzinfo=timezone.utc)
    ps = [p for p in await s.poll(now, now) if p.session_id.startswith("jfplayed:")]
    assert [p.item_key for p in ps] == ["ep4"]
    assert ps[0].decision.review and ps[0].decision.reason == "played_flag_only"
    assert ps[0].event.quality == "estimate" and round(ps[0].event.duration_seconds) == 1447
    assert s.db.get_cursor("jellyfin", "played_cursor").startswith("2026-11-02T12:30")
    await s.aclose()
