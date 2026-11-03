from datetime import datetime, timezone

from fakes import FakeKechimochi
from kbridge import config
from kbridge.filters import Decision
from kbridge.service import PollStats
from test_resolver_sync import bridge, meta, polled


def st():
    return PollStats("abs", datetime.now(timezone.utc))


def held(m, session="s1", minutes=30, reason="played_flag_only"):
    p = polled(m, session=session, minutes=minutes)
    p.decision = Decision.hold(reason)
    return p


async def test_hold_then_approve_logs_and_sticks():
    km = FakeKechimochi()
    b = bridge(km)
    await b.ingest(held(meta()), st())
    await b.sync()
    assert not km.logs
    r = b.db.one("SELECT * FROM reviews")
    assert r["status"] == "pending" and r["reason"] == "played_flag_only"
    assert await b.approve("abs", "s1", "ep")
    await b.sync()
    assert [l["duration_minutes"] for l in km.logs.values()] == [30]
    await b.ingest(held(meta(), minutes=45), st())     # source still says "review", but you approved it
    await b.sync()
    assert [l["duration_minutes"] for l in km.logs.values()] == [45]


async def test_reject_is_permanent():
    km = FakeKechimochi()
    b = bridge(km)
    await b.ingest(held(meta()), st())
    assert b.reject("abs", "s1", "ep")
    await b.ingest(held(meta()), st())
    await b.sync()
    assert not km.logs
    assert b.db.one("SELECT reason FROM skips")["reason"] == "rejected"
    assert b.db.one("SELECT status FROM reviews")["status"] == "rejected"


async def test_pending_review_updates_as_session_grows():
    b = bridge(FakeKechimochi())
    await b.ingest(held(meta(), minutes=10), st())
    await b.ingest(held(meta(), minutes=25), st())
    import json
    ev = json.loads(b.db.one("SELECT event_json FROM reviews")["event_json"])
    assert ev["duration_seconds"] == 25 * 60


async def test_long_session_switch():
    raw = {"mode": "live", "long_sessions": {"hours": 4, "action": "review"},
           "sources": {"abs": {"enabled": True, "url": "http://abs", "api_key": "k"}}}
    km = FakeKechimochi()
    b = bridge(km, config.parse(raw, {}))
    await b.ingest(polled(meta(), session="long", minutes=600), st())
    await b.ingest(polled(meta(), session="short", minutes=30), st())
    await b.sync()
    assert [l["duration_minutes"] for l in km.logs.values()] == [30]
    assert b.db.one("SELECT reason FROM reviews WHERE session_id='long'")["reason"] == "long_session"


async def test_log_anyway_for_a_skipped_session():
    km = FakeKechimochi()
    b = bridge(km)
    p = polled(meta(), session="short", minutes=1.4)
    p.decision = Decision.skip("too_short")
    await b.ingest(p, st())
    assert b.db.one("SELECT event_json FROM skips")["event_json"]
    assert await b.approve("abs", "short", "ep")
    await b.sync()
    assert [l["duration_minutes"] for l in km.logs.values()] == [1]
    assert b.db.one("SELECT count(*) n FROM skips")["n"] == 0


def test_unknown_language_switch_values():
    from kbridge import filters
    c = config.parse({"sources": {"abs": {"enabled": True, "url": "x", "api_key": "k", "languages": ["ja"],
                                          "unknown_language": "review", "default_track": True}}}, {})
    lib = c.source("abs").library("x", "lib")
    assert filters.evaluate(c.source("abs"), lib, None, 600, 2, True).review
    assert filters.evaluate(c.source("abs"), lib, None, 30, 2, True).reason == "too_short"   # hard skip wins
    old = config.parse({"sources": {"abs": {"unknown_language": "include"}}}, {})
    assert old.source("abs").unknown_language == "log"
