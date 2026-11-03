"""Normalised data passed from source adapters to the resolver and sync worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class SeriesMeta:
    """One kechimochi media entry's worth of source metadata."""
    source: str
    series_key: str
    title: str
    library_id: str
    library_name: str
    content_type: str
    activity_type: str
    description: str = ""
    language: str | None = None          # ISO 639-1
    alt_titles: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)   # readable extra_data, in display order
    cover_ref: str | None = None          # adapter-specific handle for fetch_cover()
    nagare_history_ids: list[str] = field(default_factory=list)  # e.g. "jellyfin|<item id>"
    # Stable IDs to link existing entries by, as "<extra_data key>.<field>": value,
    # e.g. {"youtube.channel_id": "UC…"} matches entries another tool made for the channel.
    match_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class ActivityEvent:
    """One source session for one item (episode, chapter, book)."""
    source: str
    session_id: str
    item_key: str
    series: SeriesMeta
    note: str
    started_at: datetime                  # timezone-aware UTC
    updated_at: datetime
    duration_seconds: float
    final: bool
    language: str | None = None
    completed: bool = False
    item_title: str = ""
    item_label: str = ""
    quality: str = "measured"             # measured | position | estimate
    flags: list[str] = field(default_factory=list)
    nagare_key: str | None = None         # Nagare's history_id for this item, for the Nagare comparison


def event_to_dict(ev: ActivityEvent) -> dict[str, Any]:
    d = asdict(ev)
    for k in ("started_at", "updated_at"):
        d[k] = d[k].isoformat()
    return d


def event_from_dict(d: dict[str, Any]) -> ActivityEvent:
    d = dict(d)
    d["series"] = SeriesMeta(**d["series"])
    for k in ("started_at", "updated_at"):
        d[k] = datetime.fromisoformat(d[k])
    return ActivityEvent(**d)
