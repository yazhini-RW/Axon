"""SQLite storage for notes.

Plain sqlite3 on purpose — this data is small, relational and boring, and keeping it
dependency-free means `axon add` can never fail because of a heavyweight import.

Times are stored as ISO 8601 strings in UTC and converted to local time only for display.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from axon.config import Settings, get_settings
from axon.models import Note, NoteKind, NoteStatus, utcnow

SCHEMA_VERSION = 4

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

_SCHEMA_V2 = """
-- Axon's own record of what's waiting on a human. Not derived from the workflow's
-- state enum: after resuming, the framework still reports IDLE_WITH_PENDING_REQUESTS
-- even once the response handler has produced its output, so it is not a reliable
-- "is this finished" signal. See docs/adr/0003-human-approval-checkpoint.md.
CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id       INTEGER NOT NULL REFERENCES notes(id),
    request_id    TEXT    NOT NULL,
    checkpoint_id TEXT    NOT NULL,
    action        TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    created_at    TEXT    NOT NULL,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
"""

# Step 9: what a hand's prepare() actually did, so `axon approvals` can show the exact
# `git push` command before the human approves it — no surprises. See
# docs/V2-PLAN.md "Practical constraints". NULL for every hand that isn't GitHubHand.
_SCHEMA_V3 = """
ALTER TABLE approvals ADD COLUMN detail TEXT;
ALTER TABLE approvals ADD COLUMN project_dir TEXT;
ALTER TABLE approvals ADD COLUMN push_url TEXT;
"""

# Step 18: the actual message a sending hand will deliver, so `axon approvals` and the
# web UI can show the human the real thing rather than a truncated summary of it. NULL
# for every hand that doesn't send a message.
_SCHEMA_V4 = """
ALTER TABLE approvals ADD COLUMN draft_subject TEXT;
ALTER TABLE approvals ADD COLUMN draft_body TEXT;
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
    # Step 10: FastAPI runs sync routes in a threadpool, so several requests can now
    # call open_db() concurrently in ways the single-process CLI never did — e.g. the
    # web UI firing GET /api/notes, /api/approvals and /api/doctor in parallel after an
    # add. SQLite's default busy behaviour is to fail immediately with "database is
    # locked" rather than wait; this makes a brief wait the default instead. Found by
    # actually running `axon serve` and driving the UI with a real browser, not by the
    # test suite, which never exercises requests running as concurrently as the UI does.
    #
    # Must be the FIRST pragma, before journal_mode below — found by a later flaky
    # test failure (Step 16): several connections racing PRAGMA journal_mode=WAL
    # itself, on a brand-new database that isn't in WAL mode yet, can still hit
    # "database is locked" if busy_timeout hasn't taken effect yet. Setting WAL mode
    # is itself a write that can contend for the lock, so busy_timeout has to be in
    # place before that first pragma runs, not after it.
    conn.execute("PRAGMA busy_timeout=5000")
    # WAL lets the `axon run` daemon read while the CLI writes, instead of one
    # blocking the other with "database is locked".
    #
    # Switching journal_mode is itself a write to the database header, and reordering
    # busy_timeout above turned out not to be a complete fix on its own: SQLite (tested
    # against 3.45.1) can still occasionally return SQLITE_BUSY on this specific pragma
    # under heavy concurrent first-access, not just wait out busy_timeout the way an
    # ordinary read/write does. A short explicit retry closes the gap busy_timeout
    # alone didn't. Confirmed by actually re-running the concurrency test dozens of
    # times, not by reasoning about it — it was intermittent, not deterministic.
    for attempt in range(5):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create or migrate the schema. Safe to call on every run — including when several
    connections race this on the very first request, which only Step 10's web server
    can do (the single-process CLI never opens two connections at once)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(_SCHEMA_V1)  # CREATE TABLE IF NOT EXISTS: safe if raced
    if version < 2:
        conn.executescript(_SCHEMA_V2)  # same
    for target, script in ((3, _SCHEMA_V3), (4, _SCHEMA_V4)):
        if version >= target:
            continue
        try:
            conn.executescript(script)
        except sqlite3.OperationalError as exc:
            # ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" in SQLite, unlike V1/V2's
            # CREATE TABLE. Two connections can both read version < 3 and both try
            # this migration; the second one loses the race, not the data — the first
            # connection's ALTER already added the column, so there's nothing left to
            # do. Found by firing concurrent requests at a brand-new database.
            if "duplicate column name" not in str(exc):
                raise
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


# --- approvals: Axon's own record of what's paused, waiting on a human -------------


@dataclass
class Approval:
    id: int
    note_id: int
    request_id: str
    checkpoint_id: str
    action: str
    status: str  # "pending" | "approved" | "rejected"
    note_text: str = ""
    detail: str = ""
    project_dir: str | None = None
    push_url: str | None = None
    draft_subject: str | None = None
    draft_body: str | None = None


def create_approval(
    conn: sqlite3.Connection,
    note_id: int,
    request_id: str,
    checkpoint_id: str,
    action: str,
    *,
    detail: str = "",
    project_dir: str | None = None,
    push_url: str | None = None,
    draft_subject: str | None = None,
    draft_body: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO approvals "
        "(note_id, request_id, checkpoint_id, action, status, created_at, "
        " detail, project_dir, push_url, draft_subject, draft_body) "
        "VALUES (?,?,?,?, 'pending', ?, ?, ?, ?, ?, ?)",
        (note_id, request_id, checkpoint_id, action, _to_db(utcnow()),
         detail, project_dir, push_url, draft_subject, draft_body),
    )
    return cur.lastrowid


def _row_to_approval(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        note_id=row["note_id"],
        request_id=row["request_id"],
        checkpoint_id=row["checkpoint_id"],
        action=row["action"],
        status=row["status"],
        note_text=row["note_text"],
        detail=row["detail"] or "",
        project_dir=row["project_dir"],
        push_url=row["push_url"],
        draft_subject=row["draft_subject"],
        draft_body=row["draft_body"],
    )


def get_approval(conn: sqlite3.Connection, approval_id: int) -> Approval | None:
    row = conn.execute(
        "SELECT a.*, n.text AS note_text FROM approvals a "
        "JOIN notes n ON n.id = a.note_id WHERE a.id = ?",
        (approval_id,),
    ).fetchone()
    return _row_to_approval(row) if row else None


def list_pending_approvals(conn: sqlite3.Connection) -> list[Approval]:
    rows = conn.execute(
        "SELECT a.*, n.text AS note_text FROM approvals a "
        "JOIN notes n ON n.id = a.note_id WHERE a.status = 'pending' ORDER BY a.id"
    ).fetchall()
    return [
        _row_to_approval(r)
        for r in rows
    ]


def resolve_approval(conn: sqlite3.Connection, approval_id: int, *, approved: bool) -> None:
    conn.execute(
        "UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ?",
        ("approved" if approved else "rejected", _to_db(utcnow()), approval_id),
    )
