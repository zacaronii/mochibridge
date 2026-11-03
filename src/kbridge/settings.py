"""Edit config.yaml from the web UI: comment-preserving, validated, atomic, applied without a restart.

config.yaml stays the single source of truth; the UI only changes the keys you touch."""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from typing import Any, Callable

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import config as config_mod
from .config import Config, ConfigError

Mutator = Callable[[CommentedMap], None]


def _rt() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def child(node: CommentedMap, key: str) -> CommentedMap:
    """node[key] as a mapping, created (or upgraded from a flow/empty value) when needed."""
    value = node.get(key)
    if not isinstance(value, CommentedMap):
        value = CommentedMap(value or {})
        node[key] = value
    return value


class SettingsStore:
    def __init__(self, path: str | Path, secrets_path: str | Path | None = None):
        self.path = Path(path)
        self.secrets_path = Path(secrets_path) if secrets_path else self.path.parent / "secrets.env"

    def env(self) -> dict[str, str]:
        return {**config_mod.load_env_file(self.secrets_path), **os.environ}

    def load(self) -> CommentedMap:
        return _rt().load(self.path.read_text(encoding="utf-8")) or CommentedMap()

    def update(self, *mutators: Mutator) -> Config:
        """Apply the changes, validate the result with the normal parser, then write atomically
        (previous file kept as config.yaml.bak). Raises ConfigError and leaves the file alone if invalid."""
        data = self.load()
        for m in mutators:
            m(data)
        buf = io.StringIO()
        _rt().dump(data, buf)
        text = buf.getvalue()
        cfg = config_mod.parse(yaml.safe_load(text) or {}, self.env())
        cfg.path = str(self.path)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
        os.replace(tmp, self.path)
        return cfg


# --------------------------------------------------------------- mutators
def set_library(source: str, library: str, **values: Any) -> Mutator:
    """values: track, content_type, activity_type, languages (list | None = any | "inherit"), unknown_language."""
    def m(data: CommentedMap) -> None:
        libs = child(child(child(data, "sources"), source), "libraries")
        rule = child(libs, library)
        for k, v in values.items():
            if v == "inherit":
                rule.pop(k, None)
            elif k == "languages" and isinstance(v, list):
                seq = CommentedSeq(v)
                seq.fa.set_flow_style()
                rule[k] = seq
            else:
                rule[k] = v
        rule.fa.set_flow_style()
    return m


def set_source(source: str, key: str, value: Any) -> Mutator:
    def m(data: CommentedMap) -> None:
        node = child(child(data, "sources"), source)
        if key == "languages" and isinstance(value, list):
            seq = CommentedSeq(value)
            seq.fa.set_flow_style()
            node[key] = seq
        else:
            node[key] = value
    return m


def set_top(path: str, value: Any) -> Mutator:
    """Dotted path below the root, e.g. "long_sessions.action" or "min_session_minutes"."""
    def m(data: CommentedMap) -> None:
        *parents, last = path.split(".")
        node = data
        for p in parents:
            node = child(node, p)
        node[last] = value
    return m


def _overrides(data: CommentedMap, kind: str) -> CommentedSeq:
    ov = child(data, "overrides")
    seq = ov.get(kind)
    if not isinstance(seq, CommentedSeq):
        seq = CommentedSeq(seq or [])
        ov[kind] = seq
    return seq


def _drop(seq: CommentedSeq, source: str, series_key: str) -> None:
    for i in reversed(range(len(seq))):
        e = seq[i]
        if isinstance(e, dict) and e.get("source") == source and str(e.get("series_key")) == series_key:
            del seq[i]


def map_series(source: str, series_key: str, media_id: int | None, note: str = "") -> Mutator:
    """Force a series onto an entry (media_id) or, with None, remove any mapping/ignore override."""
    def m(data: CommentedMap) -> None:
        for kind in ("map", "ignore"):
            _drop(_overrides(data, kind), source, series_key)
        if media_id is not None:
            entry = CommentedMap(source=source, series_key=series_key, media_id=int(media_id))
            entry.fa.set_flow_style()
            seq = _overrides(data, "map")
            seq.append(entry)
            if note:
                seq.yaml_add_eol_comment(note, len(seq) - 1)
    return m


def ignore_series(source: str, series_key: str, note: str = "") -> Mutator:
    def m(data: CommentedMap) -> None:
        for kind in ("map", "ignore"):
            _drop(_overrides(data, kind), source, series_key)
        entry = CommentedMap(source=source, series_key=series_key)
        entry.fa.set_flow_style()
        seq = _overrides(data, "ignore")
        seq.append(entry)
        if note:
            seq.yaml_add_eol_comment(note, len(seq) - 1)
    return m


__all__ = ["SettingsStore", "ConfigError", "set_library", "set_source", "set_top", "map_series", "ignore_series"]
