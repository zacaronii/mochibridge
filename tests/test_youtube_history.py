import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fakes import FakeKechimochi, entry
from kbridge import config
from kbridge.db import DB, iso
from kbridge.service import Bridge, PollStats
from kbridge.sources import youtube as yt
from kbridge.sources import youtube_history as yh
from test_youtube_sessions import FakeMeta, VIDEOS

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "youtube" / "history_page_trimmed.json"
T0 = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)          # Monday
KAWASE = {"youtube": {"channel_id": "UCy3Z4a5B6c7D8e9F0g1H2iJ", "channel_url": "https://www.youtube.com/@example_kawase"}}
MORI = {"youtube": {"channel_id": "UCk4L5m6N7o8P9q0R1s2T3uV", "channel_url": "https://www.youtube.com/@example_mori"}}


def item(vid, percent, length=1200.0, channel_id="UCy3Z4a5B6c7D8e9F0g1H2iJ", channel="カワセ", title="動画タイトル",
         section="Today", short=False):
    return yh.HistoryItem(vid, title, channel_id, channel, length, percent, section, short)


def make():
    cfg = config.parse({"mode": "live", "sources": {"youtube": {"enabled": True, "languages": ["ja"]}}}, {})
    db = DB(":memory:")
    km = FakeKechimochi([entry(17, "カワセ", extra=KAWASE, content_type="Youtube Video"),
                         entry(19, "モリ工房", extra=MORI, content_type="Youtube Video")])
    meta = FakeMeta()
    src = yt.YouTubeSource(cfg, db, meta)
    src.history_reader = yh.HistoryReader(cfg, db, meta, Path("/nonexistent"))
    return Bridge(cfg, db, km, {"youtube": src}), km, src.history_reader


async def step(b, reader, items, now):
    """One successful history poll at `now`, ingested and synced."""
    out = await reader.process(items, now)
    reader._health("ok", None, now)
    st = PollStats("youtube", now)
    for p in out:
        await b.ingest(p, st)
    await b.sync()
    return out


def test_parse_real_fixture():
    items = yh.parse_history(json.loads(FIX.read_text()))
    assert [(i.video_id, i.percent, i.length, i.channel_id, i.section) for i in items] == [
        ("Xq3Lm-T7vPa", 10.0, 16693.0, "UCa1B2c3D4e5F6g7H8i9J0kL", "Wednesday"),
        ("Rw8Hn2Kd5Ys", 22.0, 1396.0, "UCm2N3o4P5q6R7s8T9u0V1wX", "Wednesday"),
        ("Gp4Tz9Bc1Qe", 25.0, 1900.0, "UCy3Z4a5B6c7D8e9F0g1H2iJ", "Oct 25"),
        ("Mv7Nf3Ux8Lo", 26.0, 3599.0, "UCk4L5m6N7o8P9q0R1s2T3uV", "Oct 25"),
        ("Mv7Nf3Ux8Lo", 26.0, 3599.0, "UCk4L5m6N7o8P9q0R1s2T3uV", "Oct 22")]   # listed once per day watched
    assert items[2].channel == "カワセ" and items[2].title.startswith("カワセが秋のセール")


def test_extract_detects_signed_out_and_format_change():
    with pytest.raises(yh.SignedOut):
        yh.extract('<script>ytcfg.set({"LOGGED_IN": false})</script>')
    with pytest.raises(yh.FormatChanged):
        yh.extract('<script>ytcfg.set({"LOGGED_IN": true})</script><html>no data</html>')


@pytest.mark.parametrize("header,expected", [
    ("Today", date(2026, 11, 2)), ("Yesterday", date(2026, 11, 1)), ("Wednesday", date(2026, 10, 28)),
    ("Oct 25", date(2026, 10, 25)), ("Oct 25, 2025", date(2025, 10, 25)), ("Dec 30", date(2025, 12, 30)),
])
def test_section_dates(header, expected):
    assert yh.section_date(header, date(2026, 11, 2)) == expected


async def test_baseline_then_new_video_and_progress_extend_one_log():
    b, km, r = make()
    assert await step(b, r, [item("old00000001", 40)], T0) == []                     # baseline: nothing logged
    assert not km.logs
    t1 = T0 + timedelta(minutes=10)
    await step(b, r, [item("new00000001", 30), item("old00000001", 40)], t1)         # 30% of 20 min = 6 min
    (log,) = km.logs.values()
    assert log["media_id"] == 17 and log["duration_minutes"] == 6                     # existing entry カワセ entry, by channel ID
    assert log["notes"] == "動画タイトル · https://youtu.be/new00000001"
    t2 = t1 + timedelta(minutes=10)
    await step(b, r, [item("new00000001", 50), item("old00000001", 40)], t2)         # +4 min, same viewing
    (log,) = km.logs.values()
    assert log["duration_minutes"] == 10
    await step(b, r, [item("new00000001", 50), item("old00000001", 40)], t2 + timedelta(minutes=10))
    assert [l["duration_minutes"] for l in km.logs.values()] == [10]                 # no change, no new log


async def test_skip_ahead_is_capped_at_elapsed_time():
    b, km, r = make()
    await step(b, r, [item("vid00000001", 10, length=3600)], T0)                     # baseline
    await step(b, r, [item("vid00000001", 90, length=3600)], T0 + timedelta(minutes=5))   # +48 min of bar in 5 min
    (log,) = km.logs.values()
    assert log["duration_minutes"] == 5
    flags = json.loads(b.db.one("SELECT meta_json FROM logs")["meta_json"])["flags"]
    assert "capped" in flags and "history" in flags


async def test_rewatch_from_start_is_a_new_viewing():
    b, km, r = make()
    await step(b, r, [item("vid00000001", 90)], T0)
    await step(b, r, [item("vid00000001", 20)], T0 + timedelta(hours=3))             # back to 20%: watched 4 min again
    (log,) = km.logs.values()
    assert log["duration_minutes"] == 4
    assert b.db.one("SELECT session_id FROM logs")["session_id"].endswith(":1")


async def test_finished_video_back_on_top_goes_to_review():
    b, km, r = make()
    await step(b, r, [item("other000001", 40), item("done0000001", 100)], T0)
    await step(b, r, [item("done0000001", 100), item("other000001", 40)], T0 + timedelta(hours=2))
    assert not km.logs
    assert b.db.one("SELECT reason FROM reviews")["reason"] == "history_rewatch"


async def test_ten_percent_first_sighting_logs_half_capped():
    """Real test 2026-11-02: 2:03 of 29:14 showed as 10%. Default logs 5% (1.5 min here), capped at 10 min."""
    b, km, r = make()
    await step(b, r, [item("old00000001", 40)], T0)
    t = T0 + timedelta(minutes=30)
    await step(b, r, [item("opened00001", 10, length=1754), item("stream00001", 10, length=16693),
                      item("old00000001", 40)], t)
    assert [l["duration_minutes"] for l in km.logs.values()] == [10]     # the 4.6 h stream: 5% capped at 10 min
    skip = b.db.one("SELECT reason, meta_json FROM skips")                # 87.7 s is under the 2-minute minimum
    assert skip["reason"] == "too_short" and json.loads(skip["meta_json"])["minutes"] == pytest.approx(1.5, abs=0.1)
    assert "min_bar" in json.loads(b.db.one("SELECT meta_json FROM logs")["meta_json"])["flags"]


async def test_min_bar_viewing_counts_up_to_the_real_position():
    """Seen at the 10% minimum, then at 24% ten minutes later (a real case): the viewing is 24% of the video,
    not the 5% guess plus 14%."""
    b, km, r = make()
    await step(b, r, [item("old00000001", 40)], T0)
    await step(b, r, [item("grows000001", 10, length=870), item("old00000001", 40)], T0 + timedelta(minutes=10))
    await step(b, r, [item("grows000001", 24, length=870), item("old00000001", 40)], T0 + timedelta(minutes=20))
    (log,) = km.logs.values()
    assert log["duration_minutes"] == round(0.24 * 870 / 60)
    assert b.db.one("SELECT cur_seconds FROM yt_history WHERE video_id='grows000001'")["cur_seconds"] \
        == pytest.approx(0.24 * 870)


async def test_skipped_min_bar_guess_is_folded_into_the_later_viewing():
    """10% (guess too short, skipped), then finished 90 min later: one log for the whole video (a real case)."""
    b, km, r = make()
    await step(b, r, [item("old00000001", 40)], T0)
    await step(b, r, [item("later000001", 10, length=877), item("old00000001", 40)], T0 + timedelta(minutes=10))
    assert not km.logs
    await step(b, r, [item("later000001", 100, length=877), item("old00000001", 40)], T0 + timedelta(minutes=100))
    (log,) = km.logs.values()
    assert log["duration_minutes"] == round(877 / 60)


@pytest.mark.parametrize("mode,expect_logs,expect_review", [("review", 0, 1), ("ignore", 0, 0), ("full", 1, 0)])
async def test_min_bar_modes(mode, expect_logs, expect_review):
    b, km, r = make()
    r.cfg.source("youtube").extra["history_min_bar"] = mode
    await step(b, r, [item("old00000001", 40)], T0)
    await step(b, r, [item("opened00001", 10, length=1754), item("old00000001", 40)], T0 + timedelta(minutes=30))
    assert len(km.logs) == expect_logs and b.db.one("SELECT count(*) n FROM reviews")["n"] == expect_review


async def test_extension_measurement_wins_over_history():
    b, km, r = make()
    await step(b, r, [item("old00000001", 40)], T0)
    r.db.execute("INSERT INTO yt_sessions VALUES ('ext-1', 'watched0001', 'extension', 400, 380, ?, ?, '{}', NULL)",
                 (iso(T0), iso(T0 + timedelta(minutes=8))))
    out = await step(b, r, [item("watched0001", 30), item("old00000001", 40)], T0 + timedelta(minutes=10))
    assert out == [] and not km.logs


async def test_english_video_goes_to_review():
    b, km, r = make()
    await step(b, r, [item("old00000001", 40)], T0)
    await step(b, r, [item("english0001", 50, channel_id="UCen", channel="Sam", title="Tech Show"),
                      item("old00000001", 40)], T0 + timedelta(minutes=15))
    assert b.db.one("SELECT reason FROM reviews")["reason"] == "language:unknown"


async def test_duplicates_counted_once_and_catch_up_dates():
    b, km, r = make()
    await step(b, r, [item("old00000001", 40)], T0)
    later = T0 + timedelta(days=2)                                                   # bridge was down for two days
    await step(b, r, [item("caught00001", 50, section="Yesterday"), item("caught00001", 50, section="Saturday"),
                      item("old00000001", 40)], later)
    (log,) = km.logs.values()
    assert log["date"] == "2026-11-03" and log["duration_minutes"] == 10              # "Yesterday" relative to 11-04


async def test_shorts_grouped_per_channel_per_day():
    b, km, r = make()
    VIDEOS["short000001"] = {"title": "ショート1", "channel_id": "UCy3Z4a5B6c7D8e9F0g1H2iJ", "channel": "カワセ", "length": 55.0}
    VIDEOS["short000002"] = {"title": "ショート2", "channel_id": "UCy3Z4a5B6c7D8e9F0g1H2iJ", "channel": "カワセ", "length": 80.0}
    await step(b, r, [item("old00000001", 40)], T0)
    await step(b, r, [item("short000001", None, None, short=True), item("short000002", None, None, short=True),
                      item("old00000001", 40)], T0 + timedelta(minutes=10))
    (log,) = km.logs.values()
    assert log["duration_minutes"] == 2 and log["notes"] == "Shorts ×2" and log["media_id"] == 17


async def test_poll_reports_signed_out_without_cookies():
    b, km, r = make()
    assert await r.poll(T0, T0) == []
    assert r.db.get_cursor("youtube", "history_state") == "signed_out"
    assert await r.poll(T0, T0 + timedelta(minutes=1)) == []                          # respects the interval


async def test_extension_arriving_after_history_replaces_the_estimate():
    """Live 2026-11-02: history polled before the extension's first heartbeat. Measured must still win."""
    b, km, r = make()
    src = b.sources["youtube"]
    await step(b, r, [item("old00000001", 40)], T0)
    t1 = T0 + timedelta(minutes=10)
    await step(b, r, [item("raced000001", 50), item("old00000001", 40)], t1)      # history logs 10 min first
    (hist,) = km.logs.values()
    assert hist["duration_minutes"] == 10
    src.record_progress("ext-r", "raced000001", 420, 600, {"title": "動画タイトル", "channel_id": "UCy3Z4a5B6c7D8e9F0g1H2iJ",
                        "channel": "カワセ"}, now=t1 + timedelta(minutes=1))
    st = PollStats("youtube", t1)
    for p in await src.poll(t1, t1 + timedelta(minutes=12)):                       # extension session, final
        await b.ingest(p, st)
    await b.sync()
    assert [l["duration_minutes"] for l in km.logs.values()] == [7]               # history log removed, measured kept
    assert b.db.one("SELECT count(*) n FROM logs WHERE source='youtube'")["n"] == 1
    # later history progress for that viewing is not logged again
    await step(b, r, [item("raced000001", 70), item("old00000001", 40)], t1 + timedelta(minutes=20))
    assert [l["duration_minutes"] for l in km.logs.values()] == [7]


def _reader_with_accounts(tmp_path, accounts):
    """A history reader whose fake YouTube serves account `accounts[i]` for X-Goog-AuthUser i."""
    import httpx
    initial = FIX.read_text().strip()
    def handler(request):
        i = int(request.headers.get("x-goog-authuser", "0"))
        if i >= len(accounts):
            return httpx.Response(302, headers={"location": "https://accounts.google.com/ServiceLogin"})
        html = (f'<script>ytcfg.set({{"LOGGED_IN":true,"DATASYNC_ID":"{accounts[i]}","SESSION_INDEX":"{i}"}});</script>'
                f'<script>var ytInitialData = {initial};</script>')
        return httpx.Response(200, text=html, request=request)
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tx\n")
    b, km, r = make()
    r.cookies_path = jar
    r.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return b, r


async def test_history_follows_the_tracked_account_between_signed_in_accounts(tmp_path):
    b, r = _reader_with_accounts(tmp_path, ["english||", "japanese||"])
    r.db.set_cursor("youtube", "tracked_account", "japanese||")
    r.db.set_cursor("youtube", "auth_user", "0")                   # the browser switched accounts
    assert await r.fetch()
    assert r.db.get_cursor("youtube", "auth_user") == "1"


async def test_history_refuses_cookies_without_the_tracked_account(tmp_path):
    b, r = _reader_with_accounts(tmp_path, ["english||"])
    r.db.set_cursor("youtube", "tracked_account", "japanese||")
    assert await r.poll(T0, T0) == []
    assert r.db.get_cursor("youtube", "history_state") == "wrong_account"
    assert r.db.get_cursor("youtube", "history_ok") is None          # the gap is caught up once it's fixed
    assert not r.db.all("SELECT * FROM yt_history")


async def test_history_without_a_known_account_reads_as_before(tmp_path):
    b, r = _reader_with_accounts(tmp_path, ["english||"])
    assert await r.fetch()
