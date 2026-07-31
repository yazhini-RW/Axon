"""SQLite storage for notes.

Plain sqlite3 on purpose — this data is small, relational and boring, and keeping it
dependency-free means `axon add` can never fail because of a heavyweight import.

Times are stored as ISO 8601 strings in UTC and converted to local time only for display.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from axon.config import Settings, get_settings
from axon.models import Note, NoteKind, NoteStatus, utcnow

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'unclassified',
    status     TEXT    NOT NULL DEFAULT 'captured',
    due_at     TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_due_at ON notes(due_at);
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status);
"""


def _to_db(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # treat a naive datetime as local, then normalise to UTC
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat()


def _from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        text=row["text"],
        kind=NoteKind(row["kind"]),
        status=NoteStatus(row["status"]),
        due_at=_from_db(row["due_at"]),
        created_at=_from_db(row["created_at"]),
    )


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL lets the `axon run` daemon read while the CLI writes, instead of one
    # blocking the other with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create or migrate the schema. Safe to call on every run."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(_SCHEMA_V1)
    if version != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextmanager
def open_db(settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    settings = settings or get_settings()
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


def add_note(conn: sqlite3.Connection, text: str) -> Note:
    """Write a note down immediately, before anything cleverer can fail."""
    text = text.strip()
    if not text:
        raise ValueError("a note cannot be empty")

    created = utcnow()
    cur = conn.execute(
        "INSERT INTO notes (text, kind, status, due_at, created_at) VALUES (?,?,?,?,?)",
        (text, NoteKind.UNCLASSIFIED.value, NoteStatus.CAPTURED.value, None, _to_db(created)),
    )
    return Note(id=cur.lastrowid, text=text, created_at=created)


def apply_classification(
    conn: sqlite3.Connection,
    note_id: int,
    kind: NoteKind,
    due_at: datetime | None = None,
    status: NoteStatus = NoteStatus.CLASSIFIED,
) -> None:
    """Record what the brain worked out about a note."""
    conn.execute(
        "UPDATE notes SET kind = ?, due_at = ?, status = ? WHERE id = ?",
        (kind.value, _to_db(due_at), status.value, note_id),
    )


def get_note(conn: sqlite3.Connection, note_id: int) -> Note | None:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _row_to_note(row) if row else None


def set_status(conn: sqlite3.Connection, note_id: int, status: NoteStatus) -> None:
    conn.execute("UPDATE notes SET status = ? WHERE id = ?", (status.value, note_id))


def pending_reminders(conn: sqlite3.Connection) -> list[Note]:
    """Reminders with a time that haven't fired yet.

    This table is the source of truth for what still needs to happen — the scheduler
    rebuilds itself from this on every start. See docs/adr/0004-scheduler-design.md.
    """
    rows = conn.execute(
        """
        SELECT * FROM notes
        WHERE kind = ? AND due_at IS NOT NULL AND status IN (?, ?)
        ORDER BY due_at
        """,
        (NoteKind.REMINDER.value, NoteStatus.CLASSIFIED.value, NoteStatus.SCHEDULED.value),
    ).fetchall()
    return [_row_to_note(r) for r in rows]


def list_notes(conn: sqlite3.Connection, limit: int = 20) -> list[Note]:
    rows = conn.execute(
        "SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_note(r) for r in rows]


def count_notes(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
