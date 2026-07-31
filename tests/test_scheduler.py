"""The scheduler: does the right thing fire, at the right time, and survive a restart?"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from axon.config import Settings
from axon.db import repo
from axon.models import NoteKind, NoteStatus, utcnow
from axon.scheduler.notify import Notification, reminder_notification, send
from axon.scheduler.runner import OVERDUE_GRACE, ReminderService


@pytest.fixture
def caught() -> list[Notification]:
    """Notifications the service tried to send, instead of real toasts."""
    return []


@pytest.fixture
def service(settings: Settings, caught: list[Notification]) -> ReminderService:
    def collect(notification: Notification) -> list[str]:
        caught.append(notification)
        return ["test"]

    svc = ReminderService(settings=settings, notifier=collect, scheduler=BackgroundScheduler())
    yield svc
    svc.stop()


def _reminder(conn: sqlite3.Connection, text: str, *, minutes: float) -> int:
    """A reminder due `minutes` from now (negative for the past)."""
    note = repo.add_note(conn, text)
    repo.apply_classification(
        conn, note.id, NoteKind.REMINDER, utcnow() + timedelta(minutes=minutes)
    )
    return note.id


# --- what gets scheduled -------------------------------------------------------------


def test_a_future_reminder_is_scheduled_not_fired(
    settings: Settings, service: ReminderService, caught: list[Notification]
) -> None:
    with repo.open_db(settings) as conn:
        note_id = _reminder(conn, "buy milk at 5pm", minutes=60)

    counts = service.sync()

    assert counts["scheduled"] == 1
    assert caught == [], "must not fire an hour early"
    assert f"note-{note_id}" in service.scheduled_job_ids()

    with repo.open_db(settings) as conn:
        assert repo.get_note(conn, note_id).status is NoteStatus.SCHEDULED


def test_facts_and_tasks_are_never_scheduled(settings: Settings, service: ReminderService) -> None:
    with repo.open_db(settings) as conn:
        fact = repo.add_note(conn, "the wifi password is on the whiteboard")
        repo.apply_classification(conn, fact.id, NoteKind.FACT)
        task = repo.add_note(conn, "fix the login bug")
        repo.apply_classification(conn, task.id, NoteKind.TASK)

    assert service.sync() == {"scheduled": 0, "fired": 0, "missed": 0}
    assert service.scheduled_job_ids() == []


# --- overdue handling ----------------------------------------------------------------


def test_a_recently_overdue_reminder_fires_at_once(
    settings: Settings, service: ReminderService, caught: list[Notification]
) -> None:
    """'call mum this morning' typed at 6pm still deserves to reach you."""
    with repo.open_db(settings) as conn:
        note_id = _reminder(conn, "call mum this morning", minutes=-120)

    counts = service.sync()

    assert counts["fired"] == 1
    assert len(caught) == 1
    assert "overdue" in caught[0].title
    assert "call mum" in caught[0].body

    with repo.open_db(settings) as conn:
        assert repo.get_note(conn, note_id).status is NoteStatus.DONE


def test_a_very_old_reminder_is_missed_not_fired(
    settings: Settings, service: ReminderService, caught: list[Notification]
) -> None:
    """Starting the daemon after a week away must not produce a wall of toasts."""
    stale = -(OVERDUE_GRACE.total_seconds() / 60) - 60
    with repo.open_db(settings) as conn:
        note_id = _reminder(conn, "last week's standup", minutes=stale)

    counts = service.sync()

    assert counts["missed"] == 1
    assert caught == [], "nothing should be shown for a week-old reminder"

    with repo.open_db(settings) as conn:
        assert repo.get_note(conn, note_id).status is NoteStatus.MISSED


# --- the important guarantees ---------------------------------------------------------


def test_syncing_twice_does_not_double_book(settings: Settings, service: ReminderService) -> None:
    with repo.open_db(settings) as conn:
        _reminder(conn, "buy milk at 5pm", minutes=60)

    service.sync()
    service.sync()
    service.sync()

    assert len(service.scheduled_job_ids()) == 1


def test_a_fired_reminder_is_not_picked_up_again(
    settings: Settings, service: ReminderService, caught: list[Notification]
) -> None:
    with repo.open_db(settings) as conn:
        _reminder(conn, "call mum this morning", minutes=-5)

    service.sync()
    service.sync()

    assert len(caught) == 1, "fired once, not once per sync"


def test_reminders_survive_a_restart(
    settings: Settings, caught: list[Notification]
) -> None:
    """SPEC criterion 6: kill the daemon, start it again, the reminder is still there."""

    def collect(notification: Notification) -> list[str]:
        caught.append(notification)
        return ["test"]

    with repo.open_db(settings) as conn:
        note_id = _reminder(conn, "buy milk at 5pm", minutes=60)

    first = ReminderService(settings=settings, notifier=collect, scheduler=BackgroundScheduler())
    first.sync()
    assert f"note-{note_id}" in first.scheduled_job_ids()
    first.stop()  # the process dies here

    second = ReminderService(settings=settings, notifier=collect, scheduler=BackgroundScheduler())
    counts = second.sync()
    try:
        assert counts["scheduled"] == 1, "rebuilt from SQLite, not from scheduler state"
        assert f"note-{note_id}" in second.scheduled_job_ids()
    finally:
        second.stop()  # shutdown clears the in-memory jobs, so assert before stopping


def test_a_due_reminder_actually_fires(
    settings: Settings, service: ReminderService, caught: list[Notification]
) -> None:
    """End to end through APScheduler itself, not just our sync logic."""
    import time

    with repo.open_db(settings) as conn:
        note_id = _reminder(conn, "buy milk", minutes=1 / 60)  # ~1 second away

    service.start()
    deadline = time.monotonic() + 15
    while not caught and time.monotonic() < deadline:
        time.sleep(0.1)

    assert caught, "the scheduler never fired the job"
    assert "buy milk" in caught[0].body

    with repo.open_db(settings) as conn:
        assert repo.get_note(conn, note_id).status is NoteStatus.DONE


# --- notifications --------------------------------------------------------------------


def test_console_always_used_even_without_a_toast() -> None:
    channels = send(reminder_notification("buy milk"), allow_toast=False)
    assert channels == ["console"]


def test_overdue_is_visible_in_the_title() -> None:
    assert "overdue" in reminder_notification("x", overdue=True).title
    assert "overdue" not in reminder_notification("x").title
