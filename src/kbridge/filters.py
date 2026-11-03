"""Stacked tracking rules: source enabled AND library tracked AND language allowed AND long enough."""

from __future__ import annotations

from dataclasses import dataclass

from . import lang
from .config import EffectiveLibrary, SourceConfig


@dataclass(frozen=True)
class Decision:
    keep: bool
    reason: str | None = None   # skip reason when keep is False
    wait: bool = False          # too short *so far*; re-check while the session can still grow
    review: bool = False        # hold for approval in the web UI (reason says why)

    @staticmethod
    def ok() -> "Decision":
        return Decision(True)

    @staticmethod
    def skip(reason: str, wait: bool = False) -> "Decision":
        return Decision(False, reason, wait)

    @staticmethod
    def hold(reason: str) -> "Decision":
        return Decision(False, reason, review=True)


def policy(action: str, reason: str) -> Decision:
    """Apply an ignore | log | review switch to an uncertain session."""
    if action == "log":
        return Decision.ok()
    if action == "review":
        return Decision.hold(reason)
    return Decision.skip(reason)


def library_decision(src: SourceConfig, library: EffectiveLibrary) -> Decision:
    if not src.enabled:
        return Decision.skip("source_disabled")
    if not library.track:
        return Decision.skip("library_untracked")
    return Decision.ok()


def language_decision(library: EffectiveLibrary, language: str | None) -> Decision:
    if library.languages is None:
        return Decision.ok()
    code = lang.normalize(language)
    if code is None:
        return policy(library.unknown_language, "language:unknown")
    if code in library.languages:
        return Decision.ok()
    return Decision.skip(f"language:{code}")


def duration_decision(seconds: float, min_minutes: float, final: bool) -> Decision:
    minutes = seconds / 60
    # kechimochi rejects 0-minute logs, so anything that rounds to 0 is never sent.
    if minutes >= min_minutes and round(minutes) >= 1:
        return Decision.ok()
    return Decision.skip("too_short", wait=not final)


def evaluate(src: SourceConfig, library: EffectiveLibrary, language: str | None,
             seconds: float, min_minutes: float, final: bool) -> Decision:
    held = None
    for d in (library_decision(src, library), language_decision(library, language),
              duration_decision(seconds, min_minutes, final)):
        if d.review:
            held = held or d       # only held if every other check passes
        elif not d.keep:
            return d
    if held:
        return held
    return Decision.ok()
