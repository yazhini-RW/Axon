"""The rule-based brain. Free, offline, deterministic.

This is a real product feature, not scaffolding — it is what lets Axon run with no API
key. Step 6 adds a Gemini brain behind the same `Brain` interface.

A warning that shaped this whole module: `dateparser` will happily turn the bare words
"we", "to" and "on" into dates. So it is never handed raw text. A strict regex finds
genuine time expressions first, and only those fragments are parsed.
"""

from __future__ import annotations

import re
import warnings
from datetime import datetime
from typing import Protocol

import dateparser

from axon.models import Classification, NoteKind

_WEEKDAYS = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|weds?|thur?s?|fri|sat|sun"

# dateparser returns None for "tomorrow morning", so vague dayparts become real clock times.
_DAYPARTS = {
    "morning": "9am",
    "afternoon": "2pm",
    "evening": "6pm",
    "tonight": "8pm",
    "night": "8pm",
    "noon": "12pm",
    "midnight": "12am",
}

# Ordered most-specific first. Every one of these requires a genuine time signal —
# none can match a bare preposition.
_TIME_PATTERNS = [
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\bat\s+\d{1,2}:\d{2}\s*(?:am|pm)?\b",
    r"\bat\s+\d{1,2}\s*(?:am|pm)\b",
    r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b",
    r"\b\d{1,2}\s*(?:am|pm)\b",
    r"\bin\s+\d+\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b",
    r"\b(?:every|next|on|this)\s+(?:" + _WEEKDAYS + r")\b",
    r"\b(?:this|next)\s+(?:week|month|year)\b",
    # Must come before bare "tomorrow", or "tomorrow morning" gets split and the
    # daypart is lost — which silently resolves to "right now tomorrow" instead of 9am.
    r"\b(?:today\s+|tomorrow\s+|this\s+)?(?:morning|afternoon|evening|night)\b",
    r"\b(?:today|tonight|tomorrow)\b",
    r"\b(?:" + _WEEKDAYS + r")\b",
]

# Acting on these needs a human OK. See docs/adr/0003-human-approval-checkpoint.md.
_RISKY_VERBS = {
    "pay", "buy", "order", "purchase", "send", "email", "push",
    "deploy", "publish", "delete", "post", "book", "transfer",
}

_ACTION_VERBS = _RISKY_VERBS | {
    "build", "call", "write", "fix", "remind", "review", "update", "create",
    "make", "schedule", "clean", "check", "finish", "read", "ask", "add",
    "prepare", "draft", "renew", "submit", "collect", "pick",
}

_TASK_PHRASES = ("remind me", "need to", "have to", "must ", "todo", "to-do", "don't forget")

# If the sentence is clearly about the past, a weekday in it is history, not a plan.
_PAST_MARKERS = (
    "yesterday", "last week", "last month", "was ", "were ", "did ",
    "chose", "decided", "met ", "learned", "used to",
)

_PARSER_SETTINGS = {"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": True}


def _find_time_spans(text: str) -> list[str]:
    """Pull out genuine time expressions, longest/most-specific first, no overlaps."""
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []

    for pattern in _TIME_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            start, end = match.span()
            if any(start < t_end and end > t_start for t_start, t_end in taken):
                continue
            taken.append((start, end))
            found.append((start, match.group(0)))

    return [phrase for _, phrase in sorted(found)]


def _normalise(phrase: str) -> str:
    """Rewrite phrasings dateparser can't handle into ones it can."""
    phrase = phrase.lower().strip()
    phrase = re.sub(r"\bevery\s+", "", phrase)
    phrase = re.sub(r"\b(?:next|on|this)\s+(?=" + _WEEKDAYS + r")", "", phrase)
    for word, clock in _DAYPARTS.items():
        phrase = re.sub(rf"\b{word}\b", f"at {clock}", phrase)
    # "this evening" becomes "this at 6pm", which parses as nothing. Make it a real day.
    phrase = re.sub(r"\bthis\s+(?=at\b)", "today ", phrase)
    return re.sub(r"\s+", " ", phrase).strip()


def extract_due(text: str) -> tuple[datetime | None, str | None]:
    """Return (when, the phrase it came from). Both None if there's no time in the text."""
    spans = _find_time_spans(text)
    if not spans:
        return None, None

    raw = " ".join(spans)
    candidates = [_normalise(raw)]
    if len(spans) > 1:  # fall back to individual fragments if the join confuses it
        candidates.extend(_normalise(s) for s in spans)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for candidate in candidates:
            if not candidate:
                continue
            parsed = dateparser.parse(candidate, settings=_PARSER_SETTINGS)
            if parsed is not None:
                return parsed, raw
    return None, raw


def _first_word(text: str) -> str:
    match = re.search(r"[a-z']+", text.lower())
    return match.group(0) if match else ""


def _looks_like_a_task(text: str) -> bool:
    lowered = text.lower()
    return _first_word(text) in _ACTION_VERBS or any(p in lowered for p in _TASK_PHRASES)


def _is_risky(text: str) -> bool:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return bool(words & _RISKY_VERBS)


class Brain(Protocol):
    """Anything that can work out what a note is."""

    name: str

    def classify(self, text: str) -> Classification: ...


class MockBrain:
    """Rules only. No network, no key, same answer every time."""

    name = "mock"

    def classify(self, text: str) -> Classification:
        lowered = text.lower()
        due, phrase = extract_due(text)
        recurring = bool(re.search(r"\bevery\b", lowered))
        risky = _is_risky(text)

        in_the_past = any(marker in lowered for marker in _PAST_MARKERS)
        if due is not None and in_the_past:
            due, phrase = None, None

        if due is not None:
            return Classification(
                kind=NoteKind.REMINDER,
                due_at=due,
                recurring=recurring,
                risky=risky,
                reason=f"found the time {phrase!r}",
            )

        if _looks_like_a_task(text):
            return Classification(
                kind=NoteKind.TASK,
                risky=risky,
                reason=f"starts with an action ({_first_word(text)!r})"
                if _first_word(text) in _ACTION_VERBS
                else "phrased as something to do",
            )

        return Classification(
            kind=NoteKind.FACT,
            risky=risky,
            reason="no time and no action, so worth remembering",
        )


def get_brain(settings=None) -> Brain:
    """Pick a brain. Gemini arrives in Step 6; for now there is only the mock."""
    return MockBrain()
