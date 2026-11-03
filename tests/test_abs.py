from datetime import datetime, timedelta, timezone

from kbridge import config
from kbridge.sources import abs as absmod

RAW = {"sources": {"abs": {
    "enabled": True, "url": "http://abs", "api_key": "k", "languages": ["ja"],
    "libraries": {"ポッドキャスト": {"track": True}, "オーディオブック": {"track": True}, "Audiobooks": {"track": False}}}}}
CFG = config.parse(RAW, {})
SRC = CFG.source("abs")


def sessions(fixture):
    data = fixture("abs/listening_sessions.json")
    return data["sessions"][0], data["sessions"][1]


def run(session, lib_name, item=None, now=None):
    rule = SRC.library(session["libraryId"], lib_name)
    now = now or datetime.fromtimestamp(session["updatedAt"] / 1000, tz=timezone.utc) + timedelta(hours=1)
    return absmod.normalize(session, SRC, rule, lib_name, item, CFG, now)


def test_podcast_episode(fixture):
    pod, _ = sessions(fixture)                                 # Byte Talk, episode "427: Tabs vs Spaces (guest)"
    pod = {**pod, "timeListening": 1500}
    item = {"media": {"episodes": [
        {"id": pod["episodeId"], "title": "427: Tabs vs Spaces (guest)", "season": "", "episode": "427"},
        {"id": "other", "title": "x", "season": "", "episode": "426"}]}}
    p = run(pod, "ポッドキャスト", item)
    ev = p.event
    assert p.decision.keep and ev is not None
    assert ev.series.series_key == f"podcast:{pod['libraryItemId']}"
    assert ev.series.title == "Byte Talk"
    assert ev.item_key == pod["episodeId"]
    assert ev.note == "427: Tabs vs Spaces (guest)"             # number already in the title, not repeated
    assert ev.series.fields["Library"] == "ABS · ポッドキャスト"
    assert ev.series.fields["Genres"] == "Technology"         # "Podcasts" dropped
    assert ev.series.fields["Episodes"] == "2"
    assert ev.series.description.startswith("ソフトウェア開発")
    assert ev.language == "ja" and ev.final and round(ev.duration_seconds / 60) == 25
    assert ev.series.content_type == "Audio" and ev.series.activity_type == "Listening"
    assert ev.series.nagare_history_ids == []                 # Nagare grouped podcasts per episode; don't link
    no_item = run(pod, "ポッドキャスト", None).event
    assert no_item.note == "427: Tabs vs Spaces (guest)"        # falls back to the session's displayTitle


def test_book_in_series(fixture):
    _, book = sessions(fixture)
    p = run(book, "オーディオブック")
    ev = p.event
    assert ev.series.series_key.startswith("series:")
    assert ev.series.title == "青山遥作品オーディオブックプロジェクト"
    assert "青山遥作品オーディオブックプロジェクト #青山遥作品" in ev.series.alt_titles
    assert ev.note == "小説　風の手紙。"                    # non-numeric sequence -> no "Vol."
    assert ev.series.fields["Narrator"] == "森 花子" and ev.series.fields["Year"] == "2018"
    assert ev.series.nagare_history_ids == [f"audiobookshelf|{book['libraryItemId']}"]
    assert round(ev.duration_seconds / 60) == 47


def test_untracked_library_and_language(fixture):
    pod, book = sessions(fixture)
    assert run(book, "Audiobooks").decision.reason == "library_untracked"
    english = {**book, "mediaMetadata": {**book["mediaMetadata"], "language": "English"}}
    assert run(english, "オーディオブック").decision.reason == "language:en"
    assert run(book, "Some new library").decision.reason == "library_untracked"   # default_track false
    orphan = {**book, "libraryId": None}
    assert absmod.normalize(orphan, SRC, SRC.library("", "(deleted library)"), "(deleted library)", None, CFG,
                            datetime.now(timezone.utc)).decision.reason == "library_untracked"


def test_short_and_null_duration(fixture):
    pod, _ = sessions(fixture)                                 # 43 s
    growing = run(pod, "ポッドキャスト", now=datetime.fromtimestamp(pod["updatedAt"] / 1000, tz=timezone.utc))
    assert growing.decision.reason == "too_short" and growing.decision.wait
    assert run(pod, "ポッドキャスト").decision.reason == "too_short" and not run(pod, "ポッドキャスト").decision.wait
    assert run({**pod, "timeListening": None}, "ポッドキャスト").decision.reason == "too_short"


def test_episode_note_formats():
    assert absmod.episode_note("Tabs vs Spaces", None, "142") == "#142 · Tabs vs Spaces"
    assert absmod.episode_note("Ep title", "3", "7") == "#3-7 · Ep title"
    assert absmod.episode_note("427: Tabs vs Spaces (guest)", None, "427") == "427: Tabs vs Spaces (guest)"
    assert absmod.episode_note("Plain", None, None) == "Plain"
    assert absmod.episode_note("第２８４回目、「コーヒー」な雑談", "3", "284") == "第２８４回目、「コーヒー」な雑談"
    assert absmod.clean_text("<p>a &amp; b</p><p>c</p>") == "a & b\nc"
