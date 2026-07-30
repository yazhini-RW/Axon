from __future__ import annotations

import sqlite3
from datetime import timezone

import pytest

from axon.config import Settings
from axon.db import repo
from axon.models import NoteKind, NoteStatus


def test_add_note_returns_id_and_defaults(conn: sqlite3.Connection) -> None:
    note = repo.add_note(conn, "buy milk at 5pm")

    assert note.id is not None
    assert note.text == "buy milk at 5pm"
    assert note.kind is NoteKind.UNCLASSIFIED, "the brain hasn't looked at it yet"
    assert note.status is NoteStatus.CAPTURED


def test_note_survives_a_round_trip(conn: sqlite3.Connection) -> None:
    added = repo.add_note(conn, "we chose Microsoft Agent Framework")
    fetched = repo.get_note(conn, added.id)

    assert fetched is not None
    assert fetched.text == added.text
    assert fetched.kind is NoteKind.UNCLASSIFIED
    assert fetched.created_at is not None


def test_created_at_is_stored_in_utc(conn: sqlite3.Connection) -> None:
    note = repo.add_note(conn, "timezone check")
    fetched = repo.get_note(conn, note.id)

    assert fetched.created_at.tzinfo is not None, "must be timezone-aware, not naive"
    assert fetched.created_at.utcoffset() == timezone.utc.utcoffset(None)


def test_whitespace_is_trimmed(conn: sqlite3.Connection) -> None:
    assert repo.add_note(conn, "   padded note  ").text == "padded note"


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_empty_notes_are_rejected(conn: sqlite3.Connection, bad: str) -> None:
    with pytest.raises(ValueError):
        repo.add_note(conn, bad)
    assert repo.count_notes(conn) == 0


def test_list_returns_newest_first_and_respects_limit(conn: sqlite3.Connection) -> None:
    for text in ["first", "second", "third"]:
        repo.add_note(conn, text)

    assert [n.text for n in repo.list_notes(conn)] == ["third", "second", "first"]
    assert [n.text for n in repo.list_notes(conn, limit=2)] == ["third", "second"]


def test_get_note_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert repo.get_note(conn, 999) is None


def test_notes_persist_after_reopening_the_database(settings: Settings) -> None:
    """A note must outlive the process that wrote it."""
    with repo.open_db(settings) as first:
        repo.add_note(first, "survive me")

    with repo.open_db(settings) as second:
        assert [n.text for n in repo.list_notes(second)] == ["survive me"]


def test_init_db_is_safe_to_run_twice(settings: Settings) -> None:
    with repo.open_db(settings) as conn:
        repo.add_note(conn, "keep me")
        repo.init_db(conn)  # as happens on every command
        assert repo.count_notes(conn) == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == repo.SCHEMA_VERSION
