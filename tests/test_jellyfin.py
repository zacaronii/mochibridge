import sqlite3
from datetime import datetime, timezone

import pytest
import respx

from kbridge import config
from kbridge.db import DB
from kbridge.sources import jellyfin as jf

JF = "http://jellyfin:8096"
UID = "user-1"
RAW = {"sources": {"jellyfin": {
    "enabled": True, "url": JF, "api_key": "k", "user": "Me", "languages": ["ja"],
    "libraries": {"Anime": {"track": True, "content_type": "Anime", "activity_type": "Watching"},
                  "Shows": {"track": True, "content_type": "Drama"}}}}}
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
SERIES = {"Id": "ser1", "Type": "Series", "Name": "Tsukimi Kissa no Futari", "ProductionYear": 2026,
          "Status": "Ended", "Genres": ["Comedy", "Romance"], "Overview": "Hana runs a small cafe.",
          "Studios": [{"Name": "Kumo Animation"}, {"Name": "kumoanimation"}, {"Name": "Example TV"}],
          "ProviderIds": {"AniDB": "91234", "Shoko Series": "701"}}


def episode(iid, s, e, name, audio=(("jpn", True), ("eng", False)), played=None):
    return {"Id": iid, "Type": "Episode", "SeriesId": "ser1", "SeriesName": "Tsukimi Kissa no Futari",
            "ParentIndexNumber": s, "IndexNumber": e, "Name": name, "RunTimeTicks": 14470400000,
            "MediaStreams": [{"Type": "Audio", "Language": l, "IsDefault": d} for l, d in audio],
            "UserData": {"Played": bool(played), "LastPlayedDate": played}}


def nagare_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE media_watch_history (history_id, media_history_id, watch_number, server_kind, item_id,"
              " title, series_name, duration_ms, last_position_ms, last_seen, audio_languages_json)")
    c.executemany("INSERT INTO media_watch_history VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def mock_jellyfin(items, played=(), plugin_start=None):
    respx.get(f"{JF}/Users").respond(json=[{"Name": "me", "Id": UID}, {"Name": "other", "Id": "u2"}])
    respx.get(f"{JF}/Library/VirtualFolders").respond(json=[
        {"Name": "Anime", "ItemId": "libA", "CollectionType": "tvshows"},
        {"Name": "Shows", "ItemId": "libS", "CollectionType": "tvshows"}])
    def items_route(request):
        params = request.url.params
        if params.get("ParentId") == "libA":
            return respx.MockResponse(json={"Items": [{"Id": "ser1", "Name": SERIES["Name"], "Type": "Series",
                                                       "ProviderIds": SERIES["ProviderIds"]}]})
        if params.get("ParentId"):
            return respx.MockResponse(json={"Items": []})
        return respx.MockResponse(json={"Items": list(played)})
    respx.get(f"{JF}/Users/{UID}/Items").mock(side_effect=items_route)
    for it in [SERIES, *items]:
        respx.get(f"{JF}/Users/{UID}/Items/{it['Id']}").respond(json=it)
    respx.get(url__regex=rf"{JF}/Users/{UID}/Items/(gone|old1e1|old1e10)").respond(404)
    respx.get(f"{JF}/Shows/ser1/Episodes").respond(json={"Items": [episode("new1e1", 1, 1, "Opening Day"), *items]})
    respx.post(f"{JF}/user_usage_stats/submit_custom_query").respond(
        json={"colums": ["MIN"], "results": [[plugin_start]], "message": ""})
    respx.post("https://graphql.anilist.co").respond(json={"data": {"Page": {"media": [
        {"id": 912345, "title": {"native": "月見喫茶のふたり", "romaji": "Tsukimi Kissa no Futari",
                                 "english": "The Two at the Moonview Cafe"}, "synonyms": []}]}}})


def source(tmp_path, rows, **cfg_extra):
    path = tmp_path / "nagare.sqlite"
    nagare_db(path, rows)
    raw = {**RAW, **cfg_extra}
    raw["sources"]["jellyfin"]["nagare_db"] = str(path)
    db = DB(":memory:")
    db.execute("INSERT INTO idmap VALUES ('anidb','91234','R','anilist','912345',NULL,'1-12','1-12')")
    db.execute("INSERT INTO idmap VALUES ('anidb','91234','R','mal','91235',NULL,'1-12','1-12')")
    return jf.JellyfinSource(config.parse(raw, {}), db)


ROWS = [
    ("jellyfin|ep5", "jellyfin|ep5", 0, "jellyfin", "ep5", "Tsukimi S02E05 - Rainy Season", "Tsukimi",
     1447040, 1447000, "2026-11-01T13:52:17.325+00:00", '["jpn","eng"]'),
    ("rewatch:1:jellyfin|ep5", "jellyfin|ep5", 1, "jellyfin", "ep5", "Tsukimi S02E05", "Tsukimi",
     1447040, 600000, "2026-11-01T15:00:00.000+00:00", '["jpn","eng"]'),
    ("jellyfin|ep4", "jellyfin|ep4", 0, "jellyfin", "ep4", "Tsukimi S02E04 - First Snow", "Tsukimi",
     1447040, 3000000, "2026-10-30T01:36:58.099Z", '["jpn","eng"]'),
    ("jellyfin|gone", "jellyfin|gone", 0, "jellyfin", "gone", "Deleted Show S01E01", "Deleted Show",
     1400000, 1400000, "2026-10-20T10:00:00Z", "[]"),
]


@respx.mock
async def test_nagare_history(tmp_path):
    items = [episode("ep5", 2, 5, "Rainy Season"), episode("ep4", 2, 4, "First Snow"),
             episode("old1", 1, 1, "Start", played="2026-10-01T12:00:00.0000000Z")]
    mock_jellyfin(items, played=[items[2], {**items[0], "UserData": {"Played": True, "LastPlayedDate": "2026-11-01T13:53:12.4883244Z"}}])
    src = source(tmp_path, ROWS)
    ps = {p.session_id: p for p in await src.history(NOW)}

    first = ps["nagare:jellyfin|ep5"]
    ev = first.event
    assert first.decision.keep and ev.note == "S02E05 · Rainy Season" and ev.quality == "position"
    assert round(ev.duration_seconds) == 1447 and ev.language == "ja" and ev.nagare_key == "jellyfin|ep5"
    m = ev.series
    assert m.series_key == "series:ser1" and m.content_type == "Anime" and m.library_name == "Anime"
    assert m.fields["Source (Anilist)"] == "https://anilist.co/anime/912345"
    assert m.fields["MAL"] == "https://myanimelist.net/anime/91235" and m.fields["AniDB"].endswith("/91234")
    assert m.fields["Studio"] == "Kumo Animation, Example TV"
    assert "月見喫茶のふたり" in m.alt_titles and "The Two at the Moonview Cafe" in m.alt_titles
    assert m.nagare_history_ids == ["jellyfin|ep4", "jellyfin|ep5"]
    assert "_anilist_id" not in m.fields

    rewatch = ps["nagare:rewatch:1:jellyfin|ep5"]
    assert rewatch.event.flags == ["rewatch"] and round(rewatch.event.duration_seconds) == 600
    assert ps["nagare:jellyfin|ep4"].event.duration_seconds == pytest.approx(1447.04)   # capped at runtime
    assert ps["nagare:jellyfin|gone"].decision.reason == "item_missing"
    est = ps["jfplayed:old1"]
    assert est.decision.reason == "played_flag_only" and est.event.quality == "estimate"
    assert "jfplayed:ep5" not in ps                                   # Nagare already has it
    await src.aclose()


@respx.mock
async def test_nagare_until_cutoff_and_english_audio(tmp_path):
    items = [episode("ep5", 2, 5, "Rainy Season"), episode("ep4", 2, 4, "x", audio=(("eng", True), ("jpn", False)))]
    mock_jellyfin(items, plugin_start="2026-10-01 12:00:00.0000000")     # plugin rows alone don't cut Nagare off
    RAW["sources"]["jellyfin"]["nagare_until"] = "2026-11-01T12:00:00Z"
    try:
        src = source(tmp_path, ROWS)
    finally:
        RAW["sources"]["jellyfin"].pop("nagare_until")
    ps = {p.session_id: p for p in await src.history(NOW)}
    assert "nagare:jellyfin|ep5" not in ps                            # after the plugin started recording
    assert ps["nagare:jellyfin|ep4"].decision.reason == "language:en"
    await src.aclose()


def test_episode_note_and_dates():
    assert jf.episode_note({"Type": "Episode", "ParentIndexNumber": 1, "IndexNumber": 3, "Name": "X"}) == "S01E03 · X"
    assert jf.episode_note({"Type": "Movie", "Name": "The Wind Letter."}) == "The Wind Letter."
    assert jf._dt("2026-11-01T13:53:12.4883244Z") == datetime(2026, 11, 1, 13, 53, 12, 488324, tzinfo=timezone.utc)


@respx.mock
async def test_recovers_watch_whose_item_id_changed(tmp_path):
    """Before a re-import the show was 'The Two at the Moonview Cafe' with other item IDs (AniList English title)."""
    items = [episode("ep5", 2, 5, "Rainy Season"), episode("ep4", 2, 4, "First Snow")]
    mock_jellyfin(items)
    rows = ROWS + [("jellyfin|old1e1", "jellyfin|old1e1", 0, "jellyfin", "old1e1",
                    "The Two at the Moonview Cafe S01E01 - Opening Day", "The Two at the Moonview Cafe",
                    1400000, 1390000, "2026-10-20T20:00:00Z", '["jpn","eng"]'),
                   ("jellyfin|old1e10", "jellyfin|old1e10", 0, "jellyfin", "old1e10",
                    "The Two at the Moonview Cafe S01E10 - Summer Festival (Part 1)", "The Two at the Moonview Cafe",
                    1400000, 1400000, "2026-10-23T20:00:00Z", '["jpn"]')]
    src = source(tmp_path, rows)
    ps = {p.session_id: p for p in await src.history(NOW)}
    p = ps["nagare:jellyfin|old1e1"]
    assert p.decision.keep and p.event.series.series_key == "series:ser1"
    assert p.event.note == "S01E01 · Opening Day" and "recovered" in p.event.flags
    assert "jellyfin|old1e1" in p.event.series.nagare_history_ids
    assert ps["nagare:jellyfin|gone"].decision.reason == "item_missing"     # no series by that name
    lost = ps["nagare:jellyfin|old1e10"]                                    # series exists, episode never linked
    assert lost.decision.keep and lost.event.series.series_key == "series:ser1"
    assert lost.event.note == "S01E10 · Summer Festival (Part 1)" and "episode_missing" in lost.event.flags
    assert lost.event.language == "ja"
    await src.aclose()


def test_audio_fallback():
    dual = {"MediaStreams": [{"Type": "Audio", "Language": "eng", "IsDefault": True},
                             {"Type": "Audio", "Language": "jpn", "IsDefault": False}]}
    assert jf.audio_language(dual) == "en"
    assert jf.audio_language(dual, prefer=["ja"]) == "ja"
    assert jf.audio_language({"MediaStreams": [{"Type": "Audio", "Language": "eng"}]}, prefer=["ja"]) == "en"
    assert jf.audio_language({"MediaStreams": []}) is None
