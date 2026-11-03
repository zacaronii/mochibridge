import json

from fakes import FakeKechimochi, entry
from kbridge import config
from kbridge.db import DB
from kbridge.migrate import Migration, cleaned_entry
from kbridge.service import Bridge
from test_report import marker


def nag(group, *hids):
    return {"nagare": {"instance_id": "x", "group_key": json.dumps(group, ensure_ascii=False),
                       "history": [{"history_id": h} for h in hids]}}


def log(lid, mid, notes="", minutes=24, date="2026-10-20"):
    return {"id": lid, "media_id": mid, "duration_minutes": minutes, "characters": 0, "date": date,
            "date_precision": "day", "activity_type": "Watching", "notes": notes}


def setup():
    km = FakeKechimochi([
        entry(7, "ガロの奇妙な旅", content_type="Anime"),                                         # user entry
        entry(13, "Garo's Peculiar Journey (2012)", "Nagare · Jellyfin · Anime · Japanese",
              nag(["jellyfin", "Garo's Peculiar Journey (2012)", "Watching", "Anime", "Japanese"], "jellyfin|j1"),
              content_type="Anime"),                                                                  # merge -> delete
        entry(28, "Tsukimi Kissa no Futari", "Nagare · Jellyfin · TV Series · Japanese",
              nag(["jellyfin", "Tsukimi Kissa no Futari", "Watching", "TV Series", "Japanese"], "jellyfin|s2"),
              content_type="TV Series"),                                                              # linked -> clean
        entry(29, '"Kumo no Ue"', "Nagare · Jellyfin · TV Series · Japanese",
              nag(["jellyfin", '"Kumo no Ue"', "Watching", "TV Series", "Japanese"], "jellyfin|o1"),
              content_type="Drama"),                                                                  # user edited type
        entry(30, "Amber Lighthouse", "Nagare · Jellyfin · TV Series · Japanese",
              nag(["jellyfin", "Amber Lighthouse", "Watching", "TV Series", "Japanese"], "jellyfin|v1")),  # milestone
        entry(31, "Kept by hand", "Nagare · Jellyfin · Anime · Japanese",
              nag(["jellyfin", "Kept by hand", "Watching", "Anime", "Japanese"], "jellyfin|k1")),     # has a manual log
    ])
    km.milestone_uids = {"uid-30"}
    km.logs = {l["id"]: l for l in [
        log(3, 7), log(45, 7, minutes=35),
        log(40, 13, marker("jellyfin|j1")), log(41, 28, marker("jellyfin|s2")),
        log(42, 29, marker("rewatch:1:jellyfin|o1")), log(43, 30, marker("jellyfin|v1")),
        log(44, 31, marker("jellyfin|k1")), log(46, 31, "my own note"),
    ]}
    km._next = 100
    cfg = config.parse({"mode": "live", "migration": {"delete_logs": [45]}}, {})
    db = DB(":memory:")
    for mid, ctype in ((28, "Anime"), (29, "Anime"), (7, "Anime")):
        db.execute("INSERT INTO series_map(source, series_key, media_id, status, title, desired_json, updated_at)"
                   " VALUES ('jellyfin', ?, ?, 'linked', 't', ?, 'now')",
                   (f"series:{mid}", mid, json.dumps({"content_type": ctype, "default_activity_type": "Watching",
                                                      "language": "Japanese", "fields": {}})))
    return km, cfg, db, Migration(cfg, db, km, Bridge(cfg, db, km, {}))


async def run_all(m):
    return (await m.step_a(), await m.step_b(), await m.step_c({}, None), await m.step_e(), await m.step_d())


async def test_plan_matches_apply():
    km, cfg, db, m = setup()
    plan = await m.plan({7, 28, 29})
    assert sorted(l["id"] for l in plan["nagare_logs"]) == [40, 41, 42, 43, 44]
    assert [d["id"] for d in plan["media_deletes"]] == [13, 30]          # 30 is re-checked for milestones at apply
    assert {c["id"] for c in plan["cleanup"]} == {28, 29, 31}
    assert plan["manual_deletes"][0]["found"] and not plan["manual_deletes"][0]["nagare"]


async def test_apply_only_touches_what_it_should():
    km, cfg, db, m = setup()
    a, b, c, e, d = await run_all(m)
    assert a == 5 and b == 1
    assert sorted(km.logs) == [3, 46]                                    # manual logs kept (except #45)
    assert [x["id"] for x in e] == [13]                                  # #30 has a milestone, #31 a manual log
    assert set(km.media) == {7, 28, 29, 30, 31}
    m28, m29 = km.media[28], km.media[29]
    assert m28["variant"] == "" and m28["content_type"] == "Anime" and "nagare" not in m28["extra_data"]
    assert m29["content_type"] == "Drama"                                # edited by the user: left alone
    assert "nagare" not in km.media[31]["extra_data"] and km.media[31]["variant"] == ""
    v = await m.verify()
    assert v["nagare_logs"] == 0 and v["logs"] == 2


async def test_rerun_is_a_no_op_and_journal_records_before_state():
    km, cfg, db, m = setup()
    await run_all(m)
    calls = len(km.calls)
    a, b, c, e, d = await run_all(m)
    assert (a, b, e, d) == (0, 0, [], [])
    assert not [x for x in km.calls[calls:] if x[0] in ("DELETE", "PUT", "POST")]
    before = json.loads(db.one("SELECT before_json FROM migration_journal WHERE target='log:45'")["before_json"])
    assert before["duration_minutes"] == 35


async def test_resume_after_interruption():
    km, cfg, db, m = setup()
    real = km.delete_log
    count = {"n": 0}

    async def flaky(lid):
        count["n"] += 1
        if count["n"] == 3:
            raise RuntimeError("container restarted")
        await real(lid)
    km.delete_log = flaky
    try:
        await m.step_a()
    except RuntimeError:
        pass
    km.delete_log = real
    assert await m.step_a() == 3                                         # the remaining Nagare logs
    assert not [l for l in km.logs.values() if "[nagare:" in l["notes"]]


def test_cleaned_entry_keeps_user_values():
    m = entry(5, "X", "Nagare · ABS · Audiobook · English",
              nag(["audiobookshelf", "X", "Listening", "Audiobook", "English"]), content_type="Audiobook")
    m["language"] = "English"
    after, changes = cleaned_entry(m, {"content_type": "Audio", "default_activity_type": "Listening",
                                       "language": "Japanese"})
    assert after["content_type"] == "Audio" and after["language"] == "Japanese" and after["variant"] == ""
    assert len(changes) == 4
