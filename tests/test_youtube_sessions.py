import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fakes import FakeKechimochi, entry
from kbridge import config
from kbridge.db import DB
from kbridge.service import Bridge, PollStats
from kbridge.sources import youtube as yt
from kbridge.web.app import cookies_txt, create_app

T0 = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
VIDEOS = {
    "Jd2Wk6Hs0Rb": {"title": "【架空ブランド初の折りたたみ端末】Example Fold開封レビュー", "channel_id": "UCw5X6y7Z8a9B0c1D2e3F4gH",
                    "channel": "ぽんぽこ", "length": 1086.0, "channel_url": "https://www.youtube.com/@example_ponpoko"},
    "EnglishVid1": {"title": "Tech Show", "channel_id": "UCen", "channel": "Sam", "length": 3600.0},
}
EXISTING_YT = {"youtube": {"channel_id": "UCw5X6y7Z8a9B0c1D2e3F4gH", "channel_url": "https://www.youtube.com/@example_ponpoko"}}


class FakeMeta:
    client = None

    async def video(self, video_id, hint=None):
        return {**VIDEOS.get(video_id, {}), **{k: v for k, v in (hint or {}).items() if v}, "video_id": video_id}

    async def channel(self, channel_id):
        return {"avatar": None, "description": "チャンネル説明"}


def make(token="secret-token"):
    cfg = config.parse({"mode": "live", "web": {"api_token": token},
                        "sources": {"youtube": {"enabled": True, "languages": ["ja"]}}}, {})
    db = DB(":memory:")
    km = FakeKechimochi([entry(27, "ぽんぽこ", extra=EXISTING_YT, content_type="Youtube Video")])
    src = yt.YouTubeSource(cfg, db, FakeMeta())
    return Bridge(cfg, db, km, {"youtube": src}), km, src


async def poll(b, now):
    st = PollStats("youtube", now)
    for p in await b.sources["youtube"].poll(now - timedelta(seconds=15), now):
        await b.ingest(p, st)
    await b.sync()


async def test_extension_heartbeats_become_one_measured_log():
    b, km, src = make()
    for i, secs in enumerate((30, 60, 95, 140, 200)):
        src.record_progress("ext-1", "Jd2Wk6Hs0Rb", secs, secs + 10, {"title": VIDEOS["Jd2Wk6Hs0Rb"]["title"]},
                            now=T0 + timedelta(seconds=30 * i))
        await poll(b, T0 + timedelta(seconds=30 * i))
    await poll(b, T0 + timedelta(minutes=13))              # idle 10+ min -> final
    (log,) = km.logs.values()
    assert log["media_id"] == 27 and log["duration_minutes"] == 3       # linked to the existing entry by channel ID
    assert log["notes"].endswith("https://youtu.be/Jd2Wk6Hs0Rb")
    row = b.db.one("SELECT final, meta_json FROM logs")
    assert row["final"] == 1 and json.loads(row["meta_json"])["quality"] == "measured"


async def test_share_logs_full_length_and_english_goes_to_review():
    b, km, src = make()
    res = await src.record_share("https://youtu.be/Jd2Wk6Hs0Rb?si=abc", None, now=T0)
    assert res["minutes"] == 18 and res["channel"] == "ぽんぽこ"
    await src.record_share("https://www.youtube.com/watch?v=EnglishVid1", 12, now=T0)
    await poll(b, T0 + timedelta(seconds=10))
    assert [l["duration_minutes"] for l in km.logs.values()] == [18]
    assert b.db.one("SELECT reason FROM reviews")["reason"] == "language:unknown"
    with pytest.raises(ValueError):
        await src.record_share("https://example.com/not-youtube", None)


def test_api_auth_progress_log_and_cookies(tmp_path):
    b, km, src = make()
    b.cfg.db_path = str(tmp_path / "bridge.sqlite")
    c = TestClient(create_app(b, None))
    body = {"session_id": "ext-9", "video_id": "Jd2Wk6Hs0Rb", "seconds": 42, "position": 100}
    assert c.post("/api/youtube/progress", json=body).status_code == 401
    assert c.post("/api/youtube/progress", json=body, headers={"authorization": "Bearer nope"}).status_code == 401
    auth = {"authorization": "Bearer secret-token"}
    assert c.post("/api/youtube/progress", json=body, headers=auth).json() == {"ok": True}
    assert b.db.one("SELECT seconds FROM yt_sessions WHERE session_id='ext-9'")["seconds"] == 42
    r = c.post("/api/youtube/log", json={"url": "https://youtu.be/Jd2Wk6Hs0Rb", "minutes": "7"}, headers=auth)
    assert r.status_code == 200 and r.json()["message"].startswith("Logged 7 min")
    assert c.post("/api/youtube/log", json={"url": "nope"}, headers=auth).status_code == 400
    ck = [{"domain": ".youtube.com", "path": "/", "secure": True, "httpOnly": True, "expirationDate": 1893456000,
           "name": "SAPISID", "value": "abc"}]
    b.db.set_cursor("youtube", "history_state", "signed_out")
    b.db.set_cursor("youtube", "history_checked", "2026-11-02T23:14:00+00:00")
    assert c.post("/api/youtube/cookies", json={"cookies": ck}, headers=auth).json() == {"ok": True}
    assert b.db.get_cursor("youtube", "history_checked") is None     # signed out -> re-read at the next poll
    b.db.set_cursor("youtube", "history_state", "ok")
    for _ in range(3):                                                   # routine rotations: counted, not logged
        assert c.post("/api/youtube/cookies", json={"cookies": ck}, headers=auth).json() == {"ok": True}
    feed = [r["message"] for r in b.db.all("SELECT message FROM events WHERE source='youtube'")]
    assert feed == ["YouTube sign-in refreshed by the extension (history reader was signed_out)"]
    assert c.get("/api/youtube/status", headers=auth).json()["shares_today"] == 4
    p = tmp_path / "youtube-cookies.txt"
    assert "SAPISID\tabc" in p.read_text() and oct(p.stat().st_mode & 0o777) == "0o600"
    assert c.get("/api/youtube/status", headers=auth).json()["cookies"] is True
    assert c.get("/api/youtube/status", headers=auth).json()["extension_latest"] is None
    import zipfile
    (tmp_path / "downloads").mkdir()
    with zipfile.ZipFile(tmp_path / "downloads" / "mochibridge-chrome.zip", "w") as z:
        z.writestr("manifest.json", '{"version": "9.1.0"}')
    assert c.get("/api/youtube/status", headers=auth).json()["extension_latest"] == "9.1.0"
    assert c.post("/api/youtube/cookies", content=b"hello", headers={**auth, "content-type": "text/plain"}).status_code == 400


def test_api_disabled_without_token():
    b, km, src = make(token=None)
    c = TestClient(create_app(b, None))
    assert c.post("/api/youtube/log", json={"url": "x"}, headers={"authorization": "Bearer x"}).status_code == 503


def test_cookies_txt_format():
    line = cookies_txt([{"domain": ".youtube.com", "path": "/", "secure": True, "httpOnly": False,
                         "expirationDate": 100.5, "name": "A", "value": "b"}])
    assert line == ".youtube.com\tTRUE\t/\tTRUE\t100\tA\tb"


def test_parse_watch_page():
    html = 'x<script>var ytInitialPlayerResponse = {"videoDetails": {"videoId": "Jd2Wk6Hs0Rb", "title": "t", ' \
           '"channelId": "UCx", "author": "ぽんぽこ", "lengthSeconds": "1086"}, "microformat": ' \
           '{"playerMicroformatRenderer": {"ownerProfileUrl": "http://www.youtube.com/@S"}}};var meta = 1;</script>'
    assert yt.parse_watch_page(html) == {"video_id": "Jd2Wk6Hs0Rb", "title": "t", "channel_id": "UCx",
                                        "channel": "ぽんぽこ", "length": 1086.0,
                                        "channel_url": "https://www.youtube.com/@S"}
    assert yt.parse_watch_page("<html>consent</html>") == {}
