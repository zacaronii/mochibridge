import json
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeKechimochi, entry
from kbridge import config, extra
from kbridge.db import DB
from kbridge.models import ActivityEvent, SeriesMeta
from kbridge.resolver import Resolver, normalize_title
from kbridge.service import Bridge, PollStats
from kbridge.filters import Decision
from kbridge.sources.base import Polled

NAGARE = {"nagare": {"group_key": "[]", "history": [
    {"history_id": "jellyfin|ep1"}, {"history_id": "rewatch:1:jellyfin|ep2"}]}}


def cfg(mode="live", **over):
    raw = {"mode": mode, "timezone": "Etc/GMT+5", "sources": {"abs": {"enabled": True, "url": "http://abs", "api_key": "k"}}, **over}
    return config.parse(raw, {})


def meta(key="podcast:1", title="おしゃべり７２％", **kw):
    return SeriesMeta(source="abs", series_key=key, title=title, library_id="L", library_name="ポッドキャスト",
                      content_type="Audio", activity_type="Listening", description=kw.pop("description", "説明"),
                      language="ja", fields={"Library": "ABS · ポッドキャスト", "Author": "ふしぎカレー"},
                      cover_ref="item-1", **kw)


def polled(m, session="s1", minutes=25.4, final=True, note="#12 · 回"):
    t = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)      # 2026-10-31 at UTC-5
    ev = ActivityEvent(source="abs", session_id=session, item_key="ep", series=m, note=note, started_at=t,
                       updated_at=t, duration_seconds=minutes * 60, final=final, language="ja")
    return Polled(session, "ep", ev, Decision.ok())


class FakeSource:
    name = "abs"

    async def fetch_cover(self, ref):
        return b"\xff\xd8jpeg", "cover.jpg", "image/jpeg"


def bridge(km, c=None):
    c = c or cfg()
    return Bridge(c, DB(":memory:"), km, {"abs": FakeSource()})


@pytest.mark.parametrize("a,b", [
    ("Kumo no Ue Season 2", "kumo no ue"), ('"Kumo no Ue"', "Kumo no Ue"), ("ポコポコ 第2期", "ポコポコ"),
    ("青山遥作品オーディオブックプロジェクト #青山遥作品", "青山遥作品オーディオブックプロジェクト#青山遥作品"),
    ("Luminaria: Beyond the Twilight", "luminaria beyond the twilight"), ("ｔｅｃｈ　ｓｈｏｗ", "Tech Show"),
])
def test_normalize_title(a, b):
    assert normalize_title(a) == normalize_title(b)


async def test_create_then_log_then_grow_is_idempotent():
    km = FakeKechimochi()
    b = bridge(km)
    st = PollStats("abs", datetime.now(timezone.utc))
    await b.ingest(polled(meta()), st)
    s1 = await b.sync()
    assert (s1.media_created, s1.logs_created, s1.covers) == (1, 1, 1)
    (mid,) = km.media
    m = km.media[mid]
    assert m["title"] == "おしゃべり７２％" and m["variant"] == "" and m["content_type"] == "Audio"
    assert json.loads(m["extra_data"])["Author"] == "ふしぎカレー"
    assert json.loads(m["extra_data"])["Sync ID"].startswith("abs:")
    (log,) = km.logs.values()
    assert (log["date"], log["duration_minutes"], log["notes"], log["media_id"]) == ("2026-10-31", 25, "#12 · 回", mid)

    calls = len(km.calls)
    await b.ingest(polled(meta()), st)                         # same data again
    s2 = await b.sync()
    assert (s2.media_created, s2.logs_created, s2.logs_updated) == (0, 0, 0)
    assert all(c[0] == "GET" for c in km.calls[calls:])       # nothing written

    await b.ingest(polled(meta(), minutes=40), st)             # session grew
    s3 = await b.sync()
    assert (s3.logs_created, s3.logs_updated) == (0, 1)
    assert next(iter(km.logs.values()))["duration_minutes"] == 40


async def test_links_by_title_and_fills_only_empty_fields():
    existing = entry(7, "おしゃべり72%", extra={"Author": "my own text", "Note": "mine"}, cover_image="/data/covers/7.jpg")
    km = FakeKechimochi([existing])
    b = bridge(km)
    await b.ingest(polled(meta()), PollStats("abs", datetime.now(timezone.utc)))
    st = await b.sync()
    assert st.media_created == 0 and st.covers == 0           # entry already has a cover
    m = km.media[7]
    data = json.loads(m["extra_data"])
    assert data["Author"] == "my own text" and data["Note"] == "mine"      # user values kept
    assert data["Library"] == "ABS · ポッドキャスト"                         # missing key filled
    assert m["description"] == "説明" and m["title"] == "おしゃべり72%"            # empty description filled, title kept
    assert km.logs and next(iter(km.logs.values()))["media_id"] == 7


async def test_bridge_updates_its_own_fields_but_not_user_edits():
    km = FakeKechimochi()
    b = bridge(km)
    st = PollStats("abs", datetime.now(timezone.utc))
    await b.ingest(polled(meta()), st)
    await b.sync()
    (mid,) = km.media
    data = json.loads(km.media[mid]["extra_data"])
    data["Author"] = "edited by hand"
    km.media[mid]["extra_data"] = json.dumps(data, ensure_ascii=False)
    m2 = meta()
    m2.fields = {**m2.fields, "Author": "new upstream", "Episodes": "300"}
    await b.ingest(polled(m2), st)
    await b.sync()
    data = json.loads(km.media[mid]["extra_data"])
    assert data["Author"] == "edited by hand" and data["Episodes"] == "300"


async def test_nagare_marker_link_and_ambiguity():
    km = FakeKechimochi([entry(3, "Tsukimi Kissa no Futari", "Nagare · Jellyfin · TV Series · Japanese", NAGARE),
                         entry(4, "The Two at the Moonview Cafe", "Nagare · Jellyfin · Anime · Japanese",
                               {"nagare": {"history": [{"history_id": "jellyfin|ep9"}]}})])
    b = bridge(km)
    r = b.resolver
    assert await r.ensure(meta("jf:s1", "Some other title", nagare_history_ids=["jellyfin|ep2"])) == "linked"
    assert b.db.one("SELECT media_id, link_method FROM series_map WHERE series_key='jf:s1'")["media_id"] == 3
    status = await r.ensure(meta("jf:s2", "x", nagare_history_ids=["jellyfin|ep1", "jellyfin|ep9"]))
    assert status == "needs_mapping"


async def test_ambiguous_title_needs_mapping_and_logs_wait():
    km = FakeKechimochi([entry(1, "Byte Talk"), entry(2, "Byte Talk", variant="other")])
    b = bridge(km)
    await b.ingest(polled(meta(title="Byte Talk")), PollStats("abs", datetime.now(timezone.utc)))
    st = await b.sync()
    assert st.logs_created == 0 and not km.logs
    assert b.db.one("SELECT status FROM series_map")["status"] == "needs_mapping"


async def test_overrides():
    km = FakeKechimochi([entry(9, "Anything")])
    c = cfg(overrides={"map": [{"source": "abs", "series_key": "podcast:1", "media_id": 9}],
                       "ignore": [{"source": "abs", "series_key": "podcast:2"}]})
    b = bridge(km, c)
    st = PollStats("abs", datetime.now(timezone.utc))
    await b.ingest(polled(meta("podcast:1")), st)
    await b.ingest(polled(meta("podcast:2", "Ignored"), session="s2"), st)
    await b.sync()
    assert [l["media_id"] for l in km.logs.values()] == [9]
    assert st.skipped == {"series_ignored": 1}


async def test_dry_run_sends_nothing_and_records_ops():
    km = FakeKechimochi([entry(5, "おしゃべり７２％")])
    b = bridge(km, cfg(mode="dry_run"))
    st = PollStats("abs", datetime.now(timezone.utc))
    await b.ingest(polled(meta()), st)
    await b.ingest(polled(meta("podcast:new", "New Podcast"), session="s2"), st)
    s = await b.sync()
    assert all(c[0] == "GET" for c in km.calls)
    ops = [(r["method"], r["path"]) for r in b.db.all("SELECT * FROM ops")]
    assert ("POST", "/api/media") in ops and ("POST", "/api/logs") in ops and ("PUT", "/api/media/5") in ops
    again = await b.sync()
    assert again.dry_ops == 0                                  # same ops not re-recorded
    assert b.db.one("SELECT count(*) n FROM logs WHERE synced_hash IS NULL")["n"] == 2


async def test_conflict_links_existing():
    km = FakeKechimochi()
    b = bridge(km)
    await b.ingest(polled(meta()), PollStats("abs", datetime.now(timezone.utc)))
    km.media[50] = entry(50, "おしゃべり７２％")                       # appears between resolve and create
    st = await b.sync()
    assert st.media_created == 0
    assert b.db.one("SELECT media_id, link_method FROM series_map")[:] == (50, "conflict")


async def test_transient_error_backs_off():
    class Down(FakeKechimochi):
        async def create_media(self, media):
            from kbridge.kechimochi import KechimochiError
            raise KechimochiError(None, "connection refused")
    b = bridge(Down())
    await b.ingest(polled(meta()), PollStats("abs", datetime.now(timezone.utc)))
    st = await b.sync()
    row = b.db.one("SELECT attempts, next_attempt_at, status FROM series_map")
    assert st.errors == 1 and row["attempts"] == 1 and row["next_attempt_at"] and row["status"] == "pending_create"
    assert (await b.sync()).errors == 0                         # not due yet


async def test_log_deleted_by_user_is_not_recreated():
    km = FakeKechimochi()
    b = bridge(km)
    st = PollStats("abs", datetime.now(timezone.utc))
    await b.ingest(polled(meta()), st)
    await b.sync()
    km.logs.clear()
    await b.ingest(polled(meta(), minutes=50), st)
    await b.sync()
    assert not km.logs


def test_extra_merge():
    out, written = extra.merge({"A": "user", "B": "old-bridge"}, {"A": "x", "B": "new", "C": "c"}, {"B": "old-bridge"})
    assert out == {"A": "user", "B": "new", "C": "c"} and written == {"B": "new", "C": "c"}


@pytest.mark.parametrize("a,b", [
    ("ガロの奇妙な旅 (TV)", "ガロの奇妙な旅"), ("Garo's Peculiar Journey (2012)", "garos peculiar journey"),
    ("Title [BD] (TV)", "title"),
])
def test_normalize_strips_release_qualifiers(a, b):
    assert normalize_title(a) == normalize_title(b)


def test_normalize_keeps_meaningful_brackets():
    assert normalize_title("Blade Runner (Final Cut)") != normalize_title("Blade Runner")


async def test_alt_titles_link_and_close_titles_only_suggest():
    km = FakeKechimochi([entry(7, "ガロの奇妙な旅", content_type="Anime"), entry(24, "ルミナリア", content_type="Manga"),
                         entry(2, "待ってください　鷺尾さん", content_type="Manga"), entry(8, "ねこネコ猫キャット")])
    b = bridge(km)
    r = b.resolver
    garo = meta("jf:garo", "Garo's Peculiar Journey (2012)")
    garo.alt_titles = ["ガロの奇妙な旅 (TV)", "Garo no Kimyou na Tabi (TV)"]
    assert await r.ensure(garo) == "linked"
    assert await r.ensure(meta("kv:neko", "ねこ　ネコ　猫　キャット")) == "linked"
    assert await r.ensure(meta("kv:lumi", "黄昏のルミナリア")) == "pending_create"
    assert await r.ensure(meta("kv:shiro", "待ってください、鷲尾さん")) == "pending_create"
    sugg = {r_["series_key"]: (r_["media_id"], r_["method"]) for r_ in b.db.all("SELECT * FROM suggestions")}
    assert sugg == {"kv:lumi": (24, "contains"), "kv:shiro": (2, "similar")}


async def test_link_preferences_user_entry_nagare_title_and_content_type():
    nag = lambda *ids: {"nagare": {"history": [{"history_id": i} for i in ids]}}
    km = FakeKechimochi([
        entry(7, "ガロの奇妙な旅", content_type="Anime"),
        entry(13, "Garo's Peculiar Journey (2012)", "Nagare · Jellyfin · Anime · Japanese", nag("jellyfin|j1")),
        entry(28, "Tsukimi Kissa no Futari", "Nagare · Jellyfin · TV Series · Japanese", nag("jellyfin|s2")),
        entry(22, "The Two at the Moonview Cafe", "Nagare · Jellyfin · Anime · Japanese", nag("jellyfin|s1")),
        entry(8, "ねこネコ猫キャット", content_type="Novel"),
        entry(25, "Neko Neko Neko Cat", "Nagare · Jellyfin · Anime · Japanese", nag("jellyfin|k1"), content_type="Anime"),
    ])
    b = bridge(km)
    r = b.resolver
    sugg = lambda key: {(x["media_id"], x["method"]) for x in b.db.all("SELECT * FROM suggestions WHERE series_key=?", (key,))}
    linked = lambda key: b.db.one("SELECT media_id FROM series_map WHERE series_key=?", (key,))["media_id"]

    garo = meta("jf:garo", "Garo's Peculiar Journey (2012)", nagare_history_ids=["jellyfin|j1"])
    garo.alt_titles = ["ガロの奇妙な旅 (TV)"]
    assert await r.ensure(garo) == "linked" and linked("jf:garo") == 7          # the user's entry wins
    assert sugg("jf:garo") == {(13, "merge")}

    tsuki = meta("jf:tsuki", "Tsukimi Kissa no Futari", nagare_history_ids=["jellyfin|s1", "jellyfin|s2"])
    tsuki.alt_titles = ["The Two at the Moonview Cafe"]
    assert await r.ensure(tsuki) == "linked" and linked("jf:tsuki") == 28            # main title beats alt title
    assert sugg("jf:tsuki") == {(22, "merge")}

    neko = meta("kv:neko", "ねこ　ネコ　猫　キャット")
    neko.alt_titles = ["Neko Neko Neko Cat"]
    neko.content_type = "Novel"
    assert await r.ensure(neko) == "linked" and linked("kv:neko") == 8
    assert sugg("kv:neko") == set()                       # the anime entry is a different work, not a duplicate


def test_heartbeat_and_health(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from kbridge.cli import cmd_health
    b = bridge(FakeKechimochi())
    assert asyncio.run(cmd_health(b, SimpleNamespace())) == 1           # no heartbeat yet
    b.heartbeat()
    assert asyncio.run(cmd_health(b, SimpleNamespace())) == 0
    b.heartbeat("ConnectError")
    assert asyncio.run(cmd_health(b, SimpleNamespace())) == 1
