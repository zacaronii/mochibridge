from datetime import datetime, timedelta, timezone

import pytest

from kbridge.sources.jfplays import Sample, counted, group_plays, interval_seconds, watched_by_language

T0 = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
GAP = timedelta(minutes=5)


def run(*steps, start_pos=0.0):
    """steps: (seconds since start, position delta since previous, paused, lang)"""
    out, t, pos = [], 0.0, start_pos
    for dt, dpos, paused, lang in steps:
        t += dt
        pos += dpos
        out.append(Sample(T0 + timedelta(seconds=t), pos, paused, lang))
    return out


@pytest.mark.parametrize("name,steps,expected", [
    ("normal play", [(0, 0, False, "ja")] + [(15, 15, False, "ja")] * 8, {"ja": 120}),
    ("reported pause", [(0, 0, False, "ja"), (15, 15, False, "ja"), (15, 5, True, "ja"), (15, 0, True, "ja"),
                        (15, 0, True, "ja"), (15, 10, False, "ja"), (15, 15, False, "ja")], {"ja": 45}),
    ("unreported pause (position frozen)", [(0, 0, False, "ja"), (15, 15, False, "ja")] + [(15, 0, False, "ja")] * 10
     + [(15, 15, False, "ja")], {"ja": 30}),
    ("forward seek while playing", [(0, 0, False, "ja"), (15, 15, False, "ja"), (15, 300, False, "ja"),
                                    (15, 15, False, "ja")], {"ja": 45}),
    ("forward seek while paused", [(0, 0, False, "ja"), (15, 0, True, "ja"), (15, 300, True, "ja"),
                                   (15, 15, False, "ja")], {"ja": 15}),
    ("backward seek", [(0, 0, False, "ja"), (15, 15, False, "ja"), (15, -200, False, "ja"),
                       (15, 15, False, "ja")], {"ja": 30}),
    ("switch to English partway", [(0, 0, False, "ja"), (15, 15, False, "ja"), (15, 15, False, "en"),
                                   (15, 15, False, "en"), (15, 15, False, "en")], {"ja": 30, "en": 30}),
])
def test_watched_by_language(name, steps, expected):
    assert watched_by_language(run(*steps), GAP) == pytest.approx(expected), name


def test_interval_ignores_gaps_longer_than_a_play_gap():
    a = Sample(T0, 0, False, "ja")
    assert interval_seconds(a, Sample(T0 + timedelta(minutes=6), 360, False, "ja"), GAP) == 0


def test_counted_language_rule():
    assert counted({"ja": 30, "en": 30}, ["ja"]) == (30, "ja")
    assert counted({"en": 300}, ["ja"]) == (0, "en")
    assert counted({None: 60}, ["ja"]) == (60, None)
    assert counted({None: 60}, ["ja"], "ignore") == (0, None)
    assert counted({"en": 10, "ja": 5}, None) == (15, "en")
    assert counted({}, ["ja"]) == (0, None)


def test_group_plays_gap_item_change_and_restart():
    s = lambda sec, pos: Sample(T0 + timedelta(seconds=sec), pos, False, "ja")
    rows = [("sess", "ep1", s(0, 0)), ("sess", "ep1", s(15, 15)),
            # bridge restarted: 2 minutes without samples, same play continues
            ("sess", "ep1", s(135, 135)), ("sess", "ep1", s(150, 150)),
            ("sess", "ep2", s(165, 0)), ("sess", "ep2", s(180, 15)),          # next episode
            ("sess", "ep2", s(900, 300)),                                         # long silence, elsewhere: new play
            ("other", "ep1", s(10, 50))]
    plays = group_plays(rows, GAP)
    assert [(p.session_key, p.item_id, len(p.samples)) for p in plays] == [
        ("other", "ep1", 1), ("sess", "ep1", 4), ("sess", "ep2", 2), ("sess", "ep2", 1)]
    assert watched_by_language(plays[1].samples, GAP) == {"ja": 150}
    assert plays[1].key == f"jf:play:sess:ep1:{int(T0.timestamp())}"



def test_resumed_play_joins_previous_fragment():
    """66 s watched, the session ended while paused, restarted 13 min later at the same
    position, 66 s more. One play of 132 s, not two skipped fragments."""
    s = lambda sec, pos, paused=False: Sample(T0 + timedelta(seconds=sec), pos, paused, None)
    rows = [("a", "ep", s(0, 227)), ("a", "ep", s(15, 242)), ("a", "ep", s(30, 257)), ("a", "ep", s(45, 272)),
            ("a", "ep", s(60, 287)), ("a", "ep", s(66, 293, True)), ("a", "ep", s(800, 293, True)),
            ("b", "ep", s(1580, 293, True)), ("b", "ep", s(1600, 300)), ("b", "ep", s(1615, 315)),
            ("b", "ep", s(1630, 330)), ("b", "ep", s(1645, 345)), ("b", "ep", s(1660, 359))]
    (p,) = group_plays(rows, GAP)
    assert p.session_key == "a" and p.key.endswith(f":{int(T0.timestamp())}")
    assert watched_by_language(p.samples, GAP) == pytest.approx({None: 66 + 66})
    far = rows[:6] + [("b", "ep", s(1600, 900)), ("b", "ep", s(1615, 915))]   # resumed somewhere else
    assert len(group_plays(far, GAP)) == 2
