"""The daemon that fires reminders when they're due.

SQLite is the source of truth, not APScheduler's job store. The scheduler is rebuilt
from the notes table on every start and refreshed periodically, which is what makes a
reminder survive the process being killed. See docs/adr/0004-scheduler-design.md.

This deliberately never touches memory (Mem0/Qdrant): local Qdrant allows one process
only, so a daemon holding it would break every `axon add` the user typed. See ADR-0002.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from axon.config import Settings, get_settings
from axon.db import repo
from axon.models import Note, NoteStatus, utcnow
from axon.scheduler.notify import Notification, reminder_notification, send

# How often to look for reminders added by another process while this one runs.
SYNC_SECONDS = 30

# A reminder this far past its time still fires, marked overdue. Anything older is
# marked missed instead — waking up to fifty stale toasts helps nobody.
OVERDUE_GRACE = timedelta(hours=24)

Notifier = Callable[[Notification], list[str]]


class ReminderService:
    def __init__(
        self,
        settings: Settings | None = None,
        notifier: Notifier | None = None,
        scheduler: BackgroundScheduler | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._notify: Notifier = notifier or send
        self._scheduler = scheduler or BackgroundScheduler()

    # --- firing ---------------------------------------------------------------------

    def fire(self, note_id: int, text: str, *, overdue: bool = False) -> None:
        self._notify(reminder_notification(text, overdue=overdue))
        with repo.open_db(self._settings) as conn:
            repo.set_status(conn, note_id, NoteStatus.DONE)

    def _job_id(self, note: Note) -> str:
        return f"note-{note.id}"

    # --- keeping the scheduler in step with the database ------------------------------

    def _ensure_running(self) -> None:
        """APScheduler trap: while the scheduler is stopped, add_job() quietly queues
        jobs in a pending list where `replace_existing` does not apply, so the same id
        can be added over and over. Scheduling is only reliable once it is running."""
        if not self._scheduler.running:
            self._scheduler.start()

    def sync(self) -> dict[str, int]:
        """Load anything new from SQLite. Safe to call repeatedly."""
        self._ensure_running()
        counts = {"scheduled": 0, "fired": 0, "missed": 0}
        now = utcnow()

        with repo.open_db(self._settings) as conn:
            pending = repo.pending_reminders(conn)

        for note in pending:
            if note.due_at > now:
                self._schedule(note)
                counts["scheduled"] += 1
            elif now - note.due_at <= OVERDUE_GRACE:
                self.fire(note.id, note.text, overdue=True)
                counts["fired"] += 1
            else:
                with repo.open_db(self._settings) as conn:
                    repo.set_status(conn, note.id, NoteStatus.MISSED)
                counts["missed"] += 1

        return counts

    def _schedule(self, note: Note) -> None:
        self._scheduler.add_job(
            self.fire,
            trigger=DateTrigger(run_date=note.due_at),
            args=[note.id, note.text],
            id=self._job_id(note),
            replace_existing=True,  # re-syncing must not double-book a reminder
            misfire_grace_time=int(OVERDUE_GRACE.total_seconds()),
        )
        with repo.open_db(self._settings) as conn:
            repo.set_status(conn, note.id, NoteStatus.SCHEDULED)

    def scheduled_job_ids(self) -> list[str]:
        return [job.id for job in self._scheduler.get_jobs()]

    # --- the daemon -------------------------------------------------------------------

    def start(self) -> dict[str, int]:
        """Start the scheduler and do the first sync. Returns what that sync did."""
        self._ensure_running()
        counts = self.sync()
        self._scheduler.add_job(
            self.sync,
            trigger="interval",
            seconds=SYNC_SECONDS,
            id="axon-sync",
            replace_existing=True,
        )
        return counts

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def run_forever(self, tick: Callable[[], None] | None = None) -> None:
        """Block until interrupted."""
        try:
            while True:
                time.sleep(1)
                if tick:
                    tick()
        except (KeyboardInterrupt, SystemExit):
            # Swallowed deliberately: ctrl-c is how you stop the daemon, and it should
            # shut down cleanly rather than print a traceback at the user.
            pass
        finally:
            self.stop()
