import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from fakes import FakeKechimochi, entry
from kbridge import settings
from kbridge.filters import Decision
from kbridge.service import PollStats
from kbridge.web.app import create_app
from test_resolver_sync import bridge, meta, polled
from test_settings import CONFIG

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ABS_API_KEY", "k")
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    store = settings.SettingsStore(p)
    km = FakeKechimochi([entry(40, "Other entry")])
    b = bridge(km)
    b.cfg.path = str(p)
    app = create_app(b, store)
    client = TestClient(app)
    import re
    token = re.search(r'name="token" value="([^"]+)"', client.get("/settings").text).group(1)
    return b, km, client, token, store


async def seed(b, km):
    st = PollStats("abs", datetime.now(timezone.utc))
    await b.ingest(polled(meta()), st)
    held = polled(meta("podcast:2", "Held Podcast"), session="h1")
    held.decision = Decision.hold("played_flag_only")
    await b.ingest(held, st)
    short = polled(meta(), session="short", minutes=1.4)
    short.decision = Decision.skip("too_short")
    await b.ingest(short, st)
    await b.sync()


async def test_pages_render(setup):
    b, km, client, token, store = setup
    await seed(b, km)
    for path in ("/", "/review", "/activity", "/activity?show=skips", "/mappings", "/settings", "/healthz"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Held Podcast" in client.get("/review").text
    assert "おしゃべり７２％" in client.get("/").text
    assert 'class="count">1<' in client.get("/").text            # pending review badge


async def test_posts_need_token_and_same_origin(setup):
    b, km, client, token, store = setup
    await seed(b, km)
    key = json.dumps(["abs", "h1", "ep"])
    assert client.post("/review", data={"token": "nope", "action": "approve", "key": key}, headers=ORIGIN).status_code == 403
    assert client.post("/review", data={"token": token, "action": "approve", "key": key},
                       headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/review", data={"token": token, "action": "approve", "key": key}).status_code == 403  # no origin
    assert not b.db.one("SELECT 1 FROM reviews WHERE status='approved'")


async def test_approve_and_log_anyway(setup):
    b, km, client, token, store = setup
    await seed(b, km)
    row = json.dumps({"key": ["abs", "h1", "ep"], "action": "approve"})
    r = client.post("/review", data={"token": token, "row": row}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "approved+1" in r.headers["location"]
    r = client.post("/activity/log-anyway", data={"token": token, "key": json.dumps(["abs", "short", "ep"])},
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    await b.sync()
    assert sorted(l["duration_minutes"] for l in km.logs.values()) == [1, 25, 25]


async def test_settings_and_mapping_posts(setup):
    b, km, client, token, store = setup
    await seed(b, km)
    r = client.post("/settings/library", data={"token": token, "source": "abs", "key": "Audiobooks", "track": "1",
                                                "content_type": "Audio", "activity_type": "Listening",
                                                "languages": "ja,en", "unknown_language": "review"},
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "err" not in r.headers["location"]
    text = store.path.read_text(encoding="utf-8")
    assert "# Japanese podcasts" in text and "Audiobooks: {track: true" in text
    assert b.cfg.source("abs").library("x", "Audiobooks").unknown_language == "review"     # applied live
    r = client.post("/settings/global", data={"token": token, "min_session_minutes": 3, "long_hours": 5,
                                               "long_action": "review"}, headers=ORIGIN, follow_redirects=False)
    assert b.cfg.long_session_action == "review" and b.cfg.min_session_minutes == 3
    r = client.post("/settings/global", data={"token": token, "min_session_minutes": 3, "long_hours": 5,
                                               "long_action": "sometimes"}, headers=ORIGIN, follow_redirects=False)
    assert "err=" in r.headers["location"]
    (lid,) = [l for l, v in km.logs.items()]
    r = client.post("/mappings", data={"token": token, "source": "abs", "series_key": "podcast:1", "action": "link",
                                        "media_id": "40", "title": "おしゃべり７２％"}, headers=ORIGIN, follow_redirects=False)
    assert "err" not in r.headers["location"]
    await b.sync()
    assert km.logs[lid]["media_id"] == 40
    assert b.cfg.override_map == [{"source": "abs", "series_key": "podcast:1", "media_id": 40}]
