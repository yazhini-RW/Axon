from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from axon.brain.classifier import MockBrain, extract_due
from axon.models import NoteKind


@pytest.fixture
def brain() -> MockBrain:
    return MockBrain()


# --- what kind of note is it? -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "buy milk at 5pm",
        "remind me tomorrow morning to call mum",
        "in 5 days remind me to build project idea X",
        "deploy to staging at 17:00",
        "review the PR this evening",
    ],
)
def test_notes_with_a_time_are_reminders(brain: MockBrain, text: str) -> None:
    result = brain.classify(text)
    assert result.kind is NoteKind.REMINDER
    assert result.due_at is not None


@pytest.mark.parametrize(
    "text",
    ["push the axon repo to github", "fix the login bug", "pay the electricity bill"],
)
def test_actions_without_a_time_are_tasks(brain: MockBrain, text: str) -> None:
    result = brain.classify(text)
    assert result.kind is NoteKind.TASK
    assert result.due_at is None


@pytest.mark.parametrize(
    "text",
    [
        "we chose Microsoft Agent Framework because it is the newest",
        "the office wifi password is on the whiteboard",
        "Kip was my VS Code pet project",
    ],
)
def test_everything_else_is_a_fact(brain: MockBrain, text: str) -> None:
    assert brain.classify(text).kind is NoteKind.FACT


def test_past_tense_is_not_a_reminder(brain: MockBrain) -> None:
    """'I met Sarah on Monday' is history, not a plan."""
    result = brain.classify("I met Sarah on Monday")
    assert result.kind is NoteKind.FACT
    assert result.due_at is None


# --- the trap that shaped this module -----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "we chose Microsoft Agent Framework because it is the newest",
        "push the axon repo to github",
        "the office wifi password is on the whiteboard",
    ],
)
def test_bare_prepositions_are_never_read_as_dates(text: str) -> None:
    """dateparser turns "we", "to" and "on" into real dates if you let it.

    Handing it raw text would make every one of these a reminder. Guard that forever.
    """
    when, _ = extract_due(text)
    assert when is None


# --- do the times come out right? ---------------------------------------------------


def test_at_5pm_is_17_00_local(brain: MockBrain) -> None:
    due = brain.classify("buy milk at 5pm").due_at
    assert due.astimezone().hour == 17
    assert due.astimezone().minute == 0


@pytest.mark.parametrize(
    ("text", "hour"),
    [
        ("call mum tomorrow morning", 9),
        ("call mum tomorrow afternoon", 14),
        ("call mum tomorrow evening", 18),
    ],
)
def test_vague_dayparts_become_real_clock_times(brain: MockBrain, text: str, hour: int) -> None:
    """dateparser returns None for "tomorrow morning" on its own."""
    due = brain.classify(text).due_at
    assert due is not None
    assert due.astimezone().hour == hour


def test_in_5_days_lands_about_5_days_out(brain: MockBrain) -> None:
    due = brain.classify("in 5 days remind me to build project idea X").due_at
    gap = due - datetime.now(timezone.utc)
    assert timedelta(days=4, hours=23) < gap < timedelta(days=5, hours=1)


def test_due_times_are_timezone_aware(brain: MockBrain) -> None:
    """A naive datetime would silently drift when scheduled."""
    assert brain.classify("buy milk at 5pm").due_at.tzinfo is not None


# --- flags ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["buy milk at 5pm", "push the axon repo to github", "pay the electricity bill"]
)
def test_risky_verbs_are_flagged(brain: MockBrain, text: str) -> None:
    assert brain.classify(text).risky is True


@pytest.mark.parametrize("text", ["fix the login bug", "the wifi password is on the whiteboard"])
def test_harmless_notes_are_not_flagged(brain: MockBrain, text: str) -> None:
    assert brain.classify(text).risky is False


@pytest.mark.parametrize(
    "text",
    [
        "the deploy key is in bitwarden",
        "the post office closes at 6",
        "my order number is 12345",
    ],
)
def test_a_fact_is_never_risky(brain: MockBrain, text: str) -> None:
    """These contain risky words but are things to remember, not things to do.

    Risk is about what Axon will *do*, and it never acts on a fact.
    """
    result = brain.classify(text)
    assert result.risky is False, f"{text!r} is a fact, nothing to approve"


def test_recurring_notes_are_flagged(brain: MockBrain) -> None:
    result = brain.classify("email the weekly report every Friday")
    assert result.recurring is True
    assert result.kind is NoteKind.REMINDER


def test_classification_always_explains_itself(brain: MockBrain) -> None:
    for text in ["buy milk at 5pm", "fix the login bug", "the sky is blue"]:
        assert brain.classify(text).reason


def test_the_brain_is_deterministic(brain: MockBrain) -> None:
    """Same note, same answer — no API call, no randomness."""
    first = brain.classify("push the axon repo to github")
    second = brain.classify("push the axon repo to github")
    assert (first.kind, first.risky) == (second.kind, second.risky)
