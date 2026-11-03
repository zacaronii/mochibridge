"""Load config.yaml (with ${ENV} substitution from the environment and secrets.env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import lang

SOURCES = ("abs", "kavita", "jellyfin", "youtube")
NO_SERVER = {"youtube"}     # sources that don't talk to a configured server (no url/api_key needed)
SOURCE_DEFAULTS = {
    "abs": {"content_type": "Audio", "activity_type": "Listening", "poll_seconds": 60},
    "kavita": {"content_type": "Manga", "activity_type": "Reading", "poll_seconds": 300},
    "jellyfin": {"content_type": "Anime", "activity_type": "Watching", "poll_seconds": 15},
    "youtube": {"content_type": "Youtube Video", "activity_type": "Watching", "poll_seconds": 15,
                "unknown_language": "review", "default_track": True},
}
# Per-source switches (first value is the default). "review" holds the item for approval in the web UI.
CHOICES = [
    ("jellyfin", "played_flags", ("ignore", "log", "review")),        # "played" with no watch data: runtime estimate
    ("jellyfin", "audio_fallback", ("default", "any")),               # played audio track unknown
    ("kavita", "generated_sessions", ("ignore", "log", "review")),    # Kavita-made sessions from bulk "mark as read"
    ("youtube", "history_min_bar", ("half", "review", "full", "ignore")),  # first sighting at YouTube's 10% minimum
]
ACTIONS = ("ignore", "log", "review")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
class _Inherit:
    """Sentinel for "use the source-level value"; survives copy/deepcopy so identity checks keep working."""

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __repr__(self) -> str:
        return "<inherit>"


_INHERIT = _Inherit()


class ConfigError(ValueError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _substitute(obj: Any, env: dict[str, str]) -> Any:
    if isinstance(obj, str):
        def repl(m: re.Match) -> str:
            if m.group(1) not in env:
                raise ConfigError(f"environment variable {m.group(1)} is not set")
            return env[m.group(1)]
        return _ENV_RE.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _substitute(v, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, env) for v in obj]
    return obj


def _languages(value: Any) -> list[str] | None:
    """None/'any' means no language filter."""
    if value is None or value == "any":
        return None
    if isinstance(value, str):
        value = [value]
    codes = [lang.normalize(v) for v in value]
    return [c for c in codes if c]


def _unknown(value: Any, where: str) -> str:
    """ignore | log | review (the older include/exclude still work)."""
    value = {"include": "log", "exclude": "ignore"}.get(value, value)
    if value not in ACTIONS:
        raise ConfigError(f"{where}: unknown_language must be one of {list(ACTIONS)}")
    return value


@dataclass(frozen=True)
class EffectiveLibrary:
    """The fully-resolved rules for one library."""
    track: bool
    content_type: str
    activity_type: str
    languages: list[str] | None
    unknown_language: str
    explicit: bool  # listed in config (vs. falling back to default_track)


@dataclass
class LibraryRule:
    track: bool | None = None
    content_type: str | None = None
    activity_type: str | None = None
    languages: Any = _INHERIT
    unknown_language: str | None = None


@dataclass
class SourceConfig:
    name: str
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    user: str = ""
    poll_seconds: int = 60
    default_track: bool = False
    languages: list[str] | None = None
    unknown_language: str = "log"
    content_type: str = "Unknown"
    activity_type: str = "None"
    libraries: dict[str, LibraryRule] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def library(self, library_id: str, library_name: str) -> EffectiveLibrary:
        rule = self.libraries.get(str(library_id)) or self.libraries.get(library_name)
        explicit = rule is not None
        rule = rule or LibraryRule()
        return EffectiveLibrary(
            track=rule.track if rule.track is not None else (True if explicit else self.default_track),
            content_type=rule.content_type or self.content_type,
            activity_type=rule.activity_type or self.activity_type,
            languages=self.languages if rule.languages is _INHERIT else rule.languages,
            unknown_language=rule.unknown_language or self.unknown_language,
            explicit=explicit,
        )


@dataclass
class Config:
    timezone: str = "UTC"
    mode: str = "dry_run"
    min_session_minutes: float = 2
    db_path: str = "/data/bridge.sqlite"
    kechimochi_url: str = "http://kechimochi:3000"
    upload_covers: bool = True
    idmap_db: str | None = None
    idmap_refresh_hours: float = 24
    anilist_titles: bool = True
    migration_delete_logs: list[int] = field(default_factory=list)
    long_session_hours: float = 4
    long_session_action: str = "log"          # log | review
    web_port: int = 8080
    web_kechimochi_url: str = ""              # kechimochi as the browser reaches it (default: kechimochi_url)
    web_api_token: str | None = None          # bearer token for /api/* (extension, iOS Shortcut)
    path: str | None = None                   # the config.yaml this was loaded from (for the settings page)
    sources: dict[str, SourceConfig] = field(default_factory=dict)
    override_map: list[dict[str, Any]] = field(default_factory=list)
    override_ignore: list[dict[str, Any]] = field(default_factory=list)

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def source(self, name: str) -> SourceConfig:
        return self.sources[name]


def _source(name: str, raw: dict[str, Any]) -> SourceConfig:
    d = SOURCE_DEFAULTS[name]
    where = f"sources.{name}"
    libs: dict[str, LibraryRule] = {}
    for key, rule in (raw.get("libraries") or {}).items():
        rule = rule or {}
        lr = LibraryRule(
            track=rule.get("track"),
            content_type=rule.get("content_type"),
            activity_type=rule.get("activity_type"),
            unknown_language=_unknown(rule["unknown_language"], f"{where}.libraries.{key}")
            if "unknown_language" in rule else None,
        )
        if "languages" in rule:
            lr.languages = _languages(rule["languages"])
        libs[str(key)] = lr
    known = {"enabled", "url", "api_key", "user", "poll_seconds", "default_track", "languages",
             "unknown_language", "content_type", "activity_type", "libraries"}
    return SourceConfig(
        name=name,
        enabled=bool(raw.get("enabled", False)),
        url=str(raw.get("url", "")).rstrip("/"),
        api_key=str(raw.get("api_key", "")),
        user=str(raw.get("user", "")),
        poll_seconds=int(raw.get("poll_seconds", d["poll_seconds"])),
        default_track=bool(raw.get("default_track", d.get("default_track", False))),
        languages=_languages(raw.get("languages")),
        unknown_language=_unknown(raw.get("unknown_language", d.get("unknown_language", "log")), where),
        content_type=raw.get("content_type", d["content_type"]),
        activity_type=raw.get("activity_type", d["activity_type"]),
        libraries=libs,
        extra={k: v for k, v in raw.items() if k not in known},
    )


def parse(raw: dict[str, Any], env: dict[str, str] | None = None) -> Config:
    raw = _substitute(raw or {}, env if env is not None else dict(os.environ))
    mode = raw.get("mode", "dry_run")
    if mode not in ("dry_run", "live"):
        raise ConfigError("mode must be 'dry_run' or 'live'")
    k = raw.get("kechimochi") or {}
    idm = raw.get("idmap") or {}
    ov = raw.get("overrides") or {}
    bf = raw.get("backfill") or {}
    sources_raw = raw.get("sources") or {}
    unknown = set(sources_raw) - set(SOURCES)
    if unknown:
        raise ConfigError(f"unknown sources: {sorted(unknown)}")
    cfg = Config(
        timezone=raw.get("timezone", "UTC"),
        mode=mode,
        min_session_minutes=float(raw.get("min_session_minutes", 2)),
        db_path=raw.get("db_path", "/data/bridge.sqlite"),
        kechimochi_url=str(k.get("base_url", "http://kechimochi:3000")).rstrip("/"),
        upload_covers=bool(k.get("upload_covers", True)),
        idmap_db=idm.get("anibridge_db"),
        idmap_refresh_hours=float(idm.get("refresh_hours", 24)),
        anilist_titles=bool(raw.get("anilist_titles", True)),
        migration_delete_logs=[int(x) for x in (raw.get("migration") or {}).get("delete_logs") or []],
        long_session_hours=float((raw.get("long_sessions") or {}).get("hours", bf.get("long_session_hours", 4))),
        long_session_action=str((raw.get("long_sessions") or {}).get("action", "log")),
        web_port=int((raw.get("web") or {}).get("port", 8080)),
        web_kechimochi_url=str((raw.get("web") or {}).get("kechimochi_url")
                               or k.get("base_url", "http://kechimochi:3000")).rstrip("/"),
        web_api_token=(str((raw.get("web") or {}).get("api_token") or "").strip() or None),
        sources={name: _source(name, sources_raw.get(name) or {}) for name in SOURCES},
        override_map=list(ov.get("map") or []),
        override_ignore=list(ov.get("ignore") or []),
    )
    if cfg.long_session_action not in ("log", "review"):
        raise ConfigError("long_sessions.action must be 'log' or 'review'")
    for name, key, allowed in CHOICES:
        value = cfg.sources[name].extra.get(key, allowed[0])
        if value not in allowed:
            raise ConfigError(f"sources.{name}.{key} must be one of {list(allowed)}")
        cfg.sources[name].extra[key] = value
    for s in cfg.sources.values():
        if s.enabled and s.name not in NO_SERVER and (not s.url or not s.api_key):
            raise ConfigError(f"sources.{s.name}: url and api_key are required when enabled")
    return cfg


def load(path: str | Path, secrets_path: str | Path | None = None) -> Config:
    path = Path(path)
    env = dict(os.environ)
    secrets = Path(secrets_path) if secrets_path else path.parent / "secrets.env"
    # Real environment wins over the file, so docker env vars can override.
    env = {**load_env_file(secrets), **env}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = parse(raw, env)
    cfg.path = str(path)
    return cfg
