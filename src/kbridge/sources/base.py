from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..filters import Decision
from ..models import ActivityEvent


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
