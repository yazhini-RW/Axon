"""Axon's core data shapes.

These live in a real importable module on purpose. Anything that can end up inside a
workflow checkpoint must NOT be defined in __main__ — checkpoint loading is allowlisted
by "module:qualname". See docs/adr/0003-human-approval-checkpoint.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class NoteKind(str, Enum):
    """What a note turns out to be. Everything starts UNCLASSIFIED."""

    UNCLASSIFIED = "unclassified"
    FACT = "fact"           # something to remember: "we chose Agent Framework"
    TASK = "task"           # something to do, no specific time
    REMINDER = "reminder"   # something to do at a specific time


class NoteStatus(str, Enum):
    CAPTURED = "captured"            # written down, not yet understood
    CLASSIFIED = "classified"        # the brain has worked out what it is
    SCHEDULED = "scheduled"          # a reminder with a job waiting to fire
    AWAITING_APPROVAL = "awaiting_approval"  # paused, needs a human OK
    DONE = "done"
    BLOCKED = "blocked"              # the human said no


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Note:
    """One thing you told Axon."""

    text: str
    id: int | None = None
    kind: NoteKind = NoteKind.UNCLASSIFIED
    status: NoteStatus = NoteStatus.CAPTURED
    due_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def due_at_local(self) -> datetime | None:
        """due_at is stored in UTC; show it in the machine's own timezone."""
        return self.due_at.astimezone() if self.due_at else None
