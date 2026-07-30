"""The brain running as a real Microsoft Agent Framework workflow."""

from __future__ import annotations

import sqlite3

import pytest

from axon.brain.workflow import CHECKPOINT_TYPES, run_capture
from axon.config import Settings
from axon.db import repo
from axon.models import ClassifiedNote, NoteKind, NoteStatus


async def test_workflow_classifies_and_hands_back_the_result(settings: Settings) -> None:
    seen: list[ClassifiedNote] = []
    result = await run_capture(1, "buy milk at 5pm", seen.append, settings=settings)

    assert result.note_id == 1
    assert result.classification.kind is NoteKind.REMINDER
    assert result.classification.due_at is not None


async def test_the_persist_step_actually_runs(settings: Settings) -> None:
    """Proves the second executor ran, not just the first."""
    seen: list[ClassifiedNote] = []
    await run_capture(7, "fix the login bug", seen.append, settings=settings)

    assert len(seen) == 1
    assert seen[0].note_id == 7
    assert seen[0].classification.kind is NoteKind.TASK


async def test_classification_reaches_the_database(settings: Settings) -> None:
    """End to end: note in, workflow runs, row updated."""
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "buy milk at 5pm")

        def persist(result: ClassifiedNote) -> None:
            repo.apply_classification(
                conn, result.note_id, result.classification.kind, result.classification.due_at
            )

        await run_capture(note.id, note.text, persist, settings=settings)

        stored = repo.get_note(conn, note.id)
        assert stored.kind is NoteKind.REMINDER
        assert stored.status is NoteStatus.CLASSIFIED
        assert stored.due_at is not None


async def test_a_fact_gets_no_due_date_in_the_database(settings: Settings) -> None:
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "the office wifi password is on the whiteboard")

        def persist(result: ClassifiedNote) -> None:
            repo.apply_classification(
                conn, result.note_id, result.classification.kind, result.classification.due_at
            )

        await run_capture(note.id, note.text, persist, settings=settings)

        stored = repo.get_note(conn, note.id)
        assert stored.kind is NoteKind.FACT
        assert stored.due_at is None


def test_checkpoint_types_are_fully_qualified_and_importable() -> None:
    """A type missing here fails silently at resume time, so assert the shape now.

    See docs/adr/0003-human-approval-checkpoint.md.
    """
    assert CHECKPOINT_TYPES, "no types registered"
    for entry in CHECKPOINT_TYPES:
        module, _, qualname = entry.partition(":")
        assert module == "axon.models", f"{entry} must not live in __main__"
        assert qualname
