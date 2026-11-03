from datetime import datetime, timezone

import httpx
import respx

from kbridge import config
from kbridge.db import DB
from kbridge.sources import kavita as kv

RAW = {"sources": {"kavita": {
    "enabled": True, "url": "http://kavita:5000", "api_key": "k", "languages": ["ja"],
    "libraries": {"漫画棚": {"track": True, "content_type": "Manga", "activity_type": "Reading"},
                  "小説棚": {"track": True, "content_type": "Novel", "activity_type": "Reading"},
                  "Manga": {"track": False}, "Books": {"track": False}}}}}
CFG = config.parse(RAW, {})
SRC = CFG.source("kavita")
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
SERIES = {"id": 501, "name": "ネジマキボーイ", "originalName": "ネジマキボーイ", "localizedName": "Wind-Up Boy",
          "sortName": "ネジマキボーイ", "libraryId": 3}
METADATA = {"summary": "Kenta was a small-time clock repairer.", "genres": [{"title": "Action"}, {"title": "Horror"}],
            "writers": [{"name": "森野タツオ"}], "releaseYear": 2018, "publicationStatus": 0, "language": "ja",
            "totalCount": 22, "webLinks": "https://anilist.co/manga/905778,https://example.com"}


def meta(lib="漫画棚", lib_id="3"):
    return kv.series_meta(SERIES, METADATA, SRC.library(lib_id, lib), lib_id, lib)


def test_series_meta():
    m = meta()
    assert m.series_key == "series:501" and m.title == "ネジマキボーイ" and "Wind-Up Boy" in m.alt_titles
    assert m.content_type == "Manga" and m.language == "ja" and m.cover_ref == "501"
    assert m.fields["Source (Anilist)"] == "https://anilist.co/manga/905778"
    assert m.fields["Library"] == "Kavita · 漫画棚" and m.fields["Author"] == "森野タツオ"
    assert m.fields["Status"] == "Ongoing" and m.fields["Volumes"] == "22" and m.fields["Year"] == "2018"
    assert m.fields["English title"] == "Wind-Up Boy"


def test_single_chapter_session(fixture):
    s = fixture("kavita/reading_history.json")[0]
    (p,) = kv.normalize({**s, "chapters": [{**s["chapters"][0], "durationSeconds": 600}]},
                        meta(), SRC, SRC.library("3", "漫画棚"), CFG, NOW)
    assert p.decision.keep and p.session_id == "kv:901" and p.item_key == "7529"
    assert p.event.note == "Vol.1" and round(p.event.duration_seconds) == 600
    assert p.event.started_at == datetime(2026, 11, 1, 2, 2, 7, 39959, tzinfo=timezone.utc)
    assert p.event.final


def test_short_chapters_and_multi_chapter(fixture):
    s = fixture("kavita/reading_history.json")[2]                 # 14 s + 110 s
    ps = kv.normalize(s, meta(), SRC, SRC.library("3", "漫画棚"), CFG, NOW)
    assert [(p.item_key, p.event.note, p.decision.reason) for p in ps] == [
        (str(s["chapters"][0]["chapterId"]), "Vol.2", "too_short"),
        (str(s["chapters"][1]["chapterId"]), "Vol.1", "too_short")]


def test_generated_session_skipped(fixture):
    s = fixture("kavita/reading_history.json")[1]
    ps = kv.normalize(s, meta("小説棚", "4"), SRC, SRC.library("4", "小説棚"), CFG, NOW)
    assert [p.decision.reason for p in ps] == ["generated", "generated"]
    assert ps[0].event.flags == ["generated"] and ps[0].event.note == "ねこ ネコ 猫 キャット ２"


def test_english_library_untracked(fixture):
    s = {**fixture("kavita/reading_history.json")[0], "libraryId": 1, "libraryName": "Manga"}
    (p,) = kv.normalize(s, None, SRC, SRC.library("1", "Manga"), CFG, NOW)
    assert p.decision.reason == "library_untracked" and p.event is None and p.info["title"] == "ネジマキボーイ"


def test_chapter_note():
    assert kv.chapter_note("Volume 3") == "Vol.3"
    assert kv.chapter_note("Chapter 12") == "Ch.12"
    assert kv.chapter_note("Volume 2 Chapter 11") == "Vol.2 Ch.11"
    assert kv.chapter_note("The Clockmaker and the Silver Key") == "The Clockmaker and the Silver Key"


@respx.mock
async def test_history_repeats_libraries_param(fixture):
    respx.get("http://kavita:5000/api/Library/libraries").respond(json=[
        {"id": 1, "name": "Manga", "type": 0}, {"id": 3, "name": "漫画棚", "type": 0}])
    route = respx.get("http://kavita:5000/api/Stats/reading-history").respond(json=[fixture("kavita/reading_history.json")[0]])
    respx.get("http://kavita:5000/api/Series/501").respond(json=SERIES)
    respx.get("http://kavita:5000/api/Series/metadata").respond(json=METADATA)
    src = kv.KavitaSource(CFG, DB(":memory:"))
    ps = await src.history(NOW)
    assert route.calls.last.request.url.params.get_list("Libraries") == ["1", "3"]
    assert len(ps) == 1 and ps[0].event.series.title == "ネジマキボーイ"
    await src.aclose()


@respx.mock
async def test_live_poll_overlap_and_growing_session(fixture):
    respx.get("http://kavita:5000/api/Library/libraries").respond(json=[{"id": 3, "name": "漫画棚", "type": 0}])
    s0 = fixture("kavita/reading_history.json")[0]
    route = respx.get("http://kavita:5000/api/Stats/reading-history")
    respx.get("http://kavita:5000/api/Series/501").respond(json=SERIES)
    respx.get("http://kavita:5000/api/Series/metadata").respond(json=METADATA)
    src = kv.KavitaSource(CFG, DB(":memory:"))
    since = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    growing = {**s0, "endTimeUtc": "2026-11-01T02:10:00", "chapters": [{**s0["chapters"][0], "durationSeconds": 300,
                                                                      "endTimeUtc": "2026-11-01T02:10:00"}]}
    route.respond(json=[growing])
    (p,) = await src.poll(since, datetime(2026, 11, 1, 2, 11, tzinfo=timezone.utc))
    assert route.calls.last.request.url.params["StartDate"] == "2026-10-31T02:00:00Z"   # one day of overlap
    assert p.decision.keep and not p.event.final and round(p.event.duration_seconds) == 300
    grown = {**growing, "chapters": [{**growing["chapters"][0], "durationSeconds": 900}]}
    route.respond(json=[grown])
    (p2,) = await src.poll(since, datetime(2026, 11, 1, 2, 45, tzinfo=timezone.utc))
    assert p2.session_id == p.session_id and p2.event.duration_seconds == 900 and p2.event.final
    await src.aclose()
