from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..filters import Decision
from ..models import ActivityEvent


LIBRARY_REFRESH = timedelta(minutes=30)      # re-list libraries this often, so new ones are picked up without a restart


def libraries_stale(fetched_at: datetime | None, max_age: timedelta = LIBRARY_REFRESH) -> bool:
    return fetched_at is None or datetime.now(timezone.utc) - fetched_at > max_age


@dataclass(frozen=True)
class Library:
    id: str
    name: str
    media_type: str


@dataclass
class Polled:
    """One session from a source and the decision about it. `event` is kept even for skipped sessions
    when the adapter could build it, so previews can show what was skipped; `info` describes sessions
    rejected before an event existed (e.g. untracked library)."""
    session_id: str
    item_key: str
    event: ActivityEvent | None
    decision: Decision
    detail: str = ""
    info: dict = field(default_factory=dict)


class Source(Protocol):
    name: str

    async def list_libraries(self) -> list[Library]: ...

    async def poll(self, since: datetime, now: datetime) -> list[Polled]: ...

    async def history(self, now: datetime) -> list[Polled]: ...

    async def fetch_cover(self, ref: str) -> tuple[bytes, str, str] | None: ...

    async def aclose(self) -> None: ...
