"""Watched time from /Sessions samples.

Clients and the Playback Reporting plugin can't be trusted to get pauses right (the plugin only looks at a
progress report every 20 s, so a short pause can be missed), so the bridge measures time
itself: an interval between two samples counts only if the playback position actually moved forward."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SEEK_SLACK = 30.0   # seconds of position change beyond wall time still treated as normal playback


@dataclass(frozen=True)
class Sample:
    ts: datetime
    position: float          # seconds into the item
    paused: bool
    audio_lang: str | None


@dataclass
class Play:
    session_key: str
    item_id: str
    samples: list[Sample] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.samples[0].ts

    @property
    def end(self) -> datetime:
        return self.samples[-1].ts

    @property
    def key(self) -> str:
        return f"jf:play:{self.session_key}:{self.item_id}:{int(self.start.timestamp())}"


def interval_seconds(a: Sample, b: Sample, max_gap: timedelta) -> float:
    """Seconds watched between two consecutive samples of the same play."""
    wall = (b.ts - a.ts).total_seconds()
    if wall <= 0 or wall > max_gap.total_seconds():
        return 0.0
    moved = b.position - a.position
    if moved <= 0:                     # paused (reported or not), stalled, or a backward seek
        return 0.0
    if moved <= wall + SEEK_SLACK:     # normal playback; a resume mid-interval counts only what played
        return min(moved, wall)
    if a.paused or b.paused:           # a forward seek while paused isn't watching
        return 0.0
    return wall                        # skipped ahead while playing: count the time that passed


def watched_by_language(samples: list[Sample], max_gap: timedelta) -> dict[str | None, float]:
    """Watched seconds per audio language (the language selected at the start of each interval)."""
    out: dict[str | None, float] = defaultdict(float)
    for a, b in zip(samples, samples[1:]):
        secs = interval_seconds(a, b, max_gap)
        if secs:
            out[a.audio_lang] += secs
    return dict(out)


def counted(by_lang: dict[str | None, float], languages: list[str] | None,
            unknown: str = "log") -> tuple[float, str | None]:
    """Seconds that pass the language rule, and the language to report for the play.

    Only time spent with an allowed audio track counts, so switching to the dub halfway counts half. If nothing
    passes, the reported language is the one listened to most, so the skip reason says why (`language:en`)."""
    if not by_lang:
        return 0.0, None
    if languages is None:
        top = max(by_lang, key=lambda k: by_lang[k])
        return sum(by_lang.values()), top
    ok = {k: v for k, v in by_lang.items() if k in languages or (k is None and unknown != "ignore")}
    if ok:
        top = max(ok, key=lambda k: (k is not None, ok[k]))
        return sum(ok.values()), top
    return 0.0, max(by_lang, key=lambda k: by_lang[k])


RESUME_GAP = timedelta(hours=1)
RESUME_TOLERANCE = 30.0   # seconds of position difference still counted as "where it stopped"


def group_plays(rows: list[tuple[str, str, Sample]], max_gap: timedelta,
                resume_gap: timedelta = RESUME_GAP) -> list[Play]:
    """rows: (session_key, item_id, sample). A play is a run of samples for one session and item with no gap
    longer than max_gap; switching items or a long silence starts a new play.

    Then plays are joined when the same item resumes within `resume_gap` at the position the previous one
    stopped (clients restart sessions after long pauses or backgrounding, and each fragment alone could fall
    under the minimum length). The joined play keeps the first play's key."""
    runs: list[Play] = []
    current: dict[str, Play] = {}
    for session_key, item_id, s in sorted(rows, key=lambda r: (r[0], r[2].ts)):
        p = current.get(session_key)
        if p is None or p.item_id != item_id or s.ts - p.end > max_gap:
            p = Play(session_key, item_id)
            runs.append(p)
            current[session_key] = p
        p.samples.append(s)
    merged: list[Play] = []
    last_by_item: dict[str, Play] = {}
    for p in sorted(runs, key=lambda p: p.start):
        prev = last_by_item.get(p.item_id)
        if (prev is not None and timedelta(0) <= p.start - prev.end <= resume_gap
                and abs(p.samples[0].position - prev.samples[-1].position) <= RESUME_TOLERANCE):
            prev.samples.extend(p.samples)
            continue
        merged.append(p)
        last_by_item[p.item_id] = p
    return sorted(merged, key=lambda p: (p.session_key, p.start))
