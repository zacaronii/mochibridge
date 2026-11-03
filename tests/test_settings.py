import json
from datetime import datetime, timezone

import pytest

from fakes import FakeKechimochi, entry
from kbridge import settings
from kbridge.config import ConfigError
from kbridge.service import PollStats
from test_resolver_sync import bridge, meta, polled

CONFIG = """# my config
mode: live
min_session_minutes: 2   # keep short clicks out
sources:
  abs:
    enabled: true
    url: http://abs
    api_key: ${ABS_API_KEY}
    languages: [ja]
    libraries:
      ポッドキャスト: {track: true, content_type: Audio}   # Japanese podcasts
      Audiobooks: {track: false}
overrides:
  map: []
  ignore: []
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ABS_API_KEY", "k")
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    return settings.SettingsStore(p)


def test_edit_keeps_comments_and_validates(store):
    cfg = store.update(settings.set_library("abs", "Audiobooks", track=True, languages=["ja", "en"]),
                       settings.set_top("long_sessions.action", "review"),
                       settings.set_source("abs", "unknown_language", "review"))
    text = store.path.read_text(encoding="utf-8")
    assert "# my config" in text and "# keep short clicks out" in text and "# Japanese podcasts" in text
    assert "${ABS_API_KEY}" in text                                   # secrets stay references
    assert cfg.source("abs").library("x", "Audiobooks").track
    assert cfg.source("abs").library("x", "Audiobooks").languages == ["ja", "en"]
    assert cfg.long_session_action == "review" and cfg.source("abs").unknown_language == "review"
    assert (store.path.parent / "config.yaml.bak").read_text(encoding="utf-8") == CONFIG
    with pytest.raises(ConfigError):
        store.update(settings.set_top("long_sessions.action", "maybe"))
    assert "action: review" in store.path.read_text(encoding="utf-8")   # invalid change not written


def test_override_mutators(store):
    store.update(settings.map_series("kavita", "series:84", 24, "黄昏のルミナリア = ルミナリア"))
    store.update(settings.map_series("kavita", "series:84", 25))
    cfg = store.update(settings.ignore_series("abs", "book:1"))
    assert cfg.override_map == [{"source": "kavita", "series_key": "series:84", "media_id": 25}]
    assert cfg.override_ignore == [{"source": "abs", "series_key": "book:1"}]
    cfg = store.update(settings.map_series("kavita", "series:84", None))
    assert cfg.override_map == []


async def test_hot_reload_changes_next_decision(store):
    from kbridge.sources.abs import normalize
    km = FakeKechimochi()
    b = bridge(km)

    class Src:
        name = "abs"
        cfg = None
        src = None

        async def fetch_cover(self, ref):
            return None
    s = Src()
    b.sources = {"abs": s}
    b.apply_config(store.update(settings.set_library("abs", "Audiobooks", track=True)))
    assert s.src.library("x", "Audiobooks").track and b.cfg.mode == "live"


async def test_remap_moves_existing_logs():
    km = FakeKechimochi([entry(40, "Other entry")])
    b = bridge(km)
    await b.ingest(polled(meta()), PollStats("abs", datetime.now(timezone.utc)))
    await b.sync()
    (lid,) = km.logs
    old_media = km.logs[lid]["media_id"]
    b.remap("abs", "podcast:1", 40)
    await b.sync()
    assert list(km.logs) == [lid] and km.logs[lid]["media_id"] == 40 != old_media   # moved, not duplicated
