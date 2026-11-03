"""Readable extra_data fields, merged without clobbering anything the user (or another tool) wrote."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import SeriesMeta

SOURCE_PREFIX = {"abs": "abs", "kavita": "kv", "jellyfin": "jf", "youtube": "yt"}


def sync_id(source: str, series_key: str) -> str:
    return f"{SOURCE_PREFIX.get(source, source)}:{hashlib.sha1(series_key.encode()).hexdigest()[:8]}"


def desired_fields(meta: SeriesMeta) -> dict[str, Any]:
    """Readable string fields, plus structured values (e.g. the `youtube: {channel_id, channel_url}`) kept
    as objects so existing entries keep their shape."""
    fields: dict[str, Any] = {}
    for k, v in meta.fields.items():
        if isinstance(v, dict):
            if v:
                fields[k] = v
        elif v is not None and str(v).strip():
            fields[k] = str(v).strip()
    fields["Sync ID"] = sync_id(meta.source, meta.series_key)
    return fields


def parse(extra_json: str | None) -> dict[str, Any]:
    try:
        value = json.loads(extra_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def merge(existing: dict[str, Any], desired: dict[str, str],
          written: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Return (new extra_data, new written map).

    `written` remembers the value the bridge last wrote for each key. A key is only (re)written when it is
    missing, or still holds exactly what the bridge wrote last time; anything else was edited by someone
    else and is left alone. Keys the bridge never wrote are always preserved."""
    out = dict(existing)
    new_written = dict(written)
    for key, value in desired.items():
        if key not in out or (key in written and out[key] == written[key]):
            out[key] = value
            new_written[key] = value
    return out, new_written
