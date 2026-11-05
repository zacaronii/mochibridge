import pytest

from kbridge import config, filters, lang

RAW = {
    "mode": "dry_run",
    "sources": {
        "abs": {
            "enabled": True, "url": "http://audiobookshelf:80/", "api_key": "${ABS_API_KEY}",
            "default_track": False, "languages": ["Japanese"],
            "libraries": {
                "ポッドキャスト": {"track": True},
                "オーディオブック": {"content_type": "Audio"},
                "Audiobooks": {"track": False},
                "lib-any": {"track": True, "languages": None},
                "lib-strict": {"track": True, "unknown_language": "exclude"},
            },
        },
    },
}


def cfg():
    return config.parse(RAW, {"ABS_API_KEY": "k"})


def test_env_substitution_and_defaults():
    c = cfg()
    abs_ = c.source("abs")
    assert abs_.api_key == "k" and abs_.url == "http://audiobookshelf:80"
    assert abs_.languages == ["ja"]
    assert abs_.content_type == "Audio" and abs_.activity_type == "Listening"
    assert not c.source("kavita").enabled


def test_missing_env_is_an_error():
    with pytest.raises(config.ConfigError):
        config.parse(RAW, {})


def test_library_resolution():
    abs_ = cfg().source("abs")
    assert abs_.library("x", "ポッドキャスト").track
    assert abs_.library("x", "オーディオブック").track          # listed without track -> tracked
    assert not abs_.library("x", "Audiobooks").track
    unlisted = abs_.library("new-id", "Brand new library")
    assert not unlisted.track and not unlisted.explicit
    assert abs_.library("lib-any", "whatever").languages is None   # matched by id, override to any


def test_stacked_filters():
    abs_ = cfg().source("abs")
    pod = abs_.library("x", "ポッドキャスト")
    ev = lambda lib, language, secs=600, final=True: filters.evaluate(abs_, lib, language, secs, 2, final)
    assert ev(pod, "ja").keep
    assert ev(pod, "Japanese").keep
    assert ev(pod, "en").reason == "language:en"
    assert ev(pod, None).keep                                   # unknown -> include by default
    assert ev(abs_.library("lib-strict", "s"), None).reason == "language:unknown"
    assert ev(abs_.library("x", "Audiobooks"), "ja").reason == "library_untracked"
    assert ev(abs_.library("lib-any", "a"), "en").keep
    short = ev(pod, "ja", secs=60, final=False)
    assert short.reason == "too_short" and short.wait
    assert not ev(pod, "ja", secs=60, final=True).wait


def test_disabled_source():
    c = cfg()
    c.source("abs").enabled = False
    lib = c.source("abs").library("x", "ポッドキャスト")
    assert filters.evaluate(c.source("abs"), lib, "ja", 600, 2, True).reason == "source_disabled"


@pytest.mark.parametrize("raw,code", [
    ("ja", "ja"), ("jpn", "ja"), ("Japanese", "ja"), ("日本語", "ja"), ("ja-JP", "ja"),
    ("eng", "en"), ("en-US", "en"), ("und", None), ("", None), (None, None), ("zh_Hant", "zh"),
])
def test_lang_normalize(raw, code):
    assert lang.normalize(raw) == code


def test_library_rules_survive_deepcopy():
    import copy
    abs_ = copy.deepcopy(cfg().source("abs"))
    assert abs_.library("x", "ポッドキャスト").languages == ["ja"]


def test_switch_validation():
    raw = {"sources": {"jellyfin": {"played_flags": "maybe"}}}
    with pytest.raises(config.ConfigError):
        config.parse(raw, {})
    c = config.parse({"sources": {}}, {})
    assert c.source("jellyfin").extra["played_flags"] == "ignore"
    assert c.source("kavita").extra["generated_sessions"] == "ignore"
    assert c.source("jellyfin").extra["audio_fallback"] == "default"


def test_kechimochi_url_environment_overrides():
    raw = {"kechimochi": {"base_url": "http://kechimochi:3000"}, "web": {"kechimochi_url": "https://k.example.com"}}
    cfg = config.parse(raw, {})
    assert (cfg.kechimochi_url, cfg.kechimochi_url_from, cfg.web_kechimochi_url) == \
        ("http://kechimochi:3000", "config", "https://k.example.com")
    cfg = config.parse(raw, {"KECHIMOCHI_URL": "http://other:3000/", "KECHIMOCHI_PUBLIC_URL": "https://o.example.com"})
    assert (cfg.kechimochi_url, cfg.kechimochi_url_from, cfg.web_kechimochi_url) == \
        ("http://other:3000", "KECHIMOCHI_URL", "https://o.example.com")
    assert config.parse({}, {"KECHIMOCHI_URL": "http://other:3000"}).web_kechimochi_url == "http://other:3000"
