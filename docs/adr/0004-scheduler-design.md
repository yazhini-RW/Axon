# ADR-0004: SQLite is the source of truth for reminders, not APScheduler's job store

- **Status:** Accepted
- **Date:** 2026-07-30

## Context

Reminders must survive the process being killed (SPEC criterion 6). The obvious way to
get that with APScheduler is `SQLAlchemyJobStore` — a persistent job store on disk.
`requirements.txt` originally described exactly that.

Two problems surfaced once the daemon was real.

**Cross-process additions.** `axon add` runs in one process, `axon run` in another. A
running scheduler decides when to next wake up from the jobs it already knows about. A
job written to a shared store by a *different* process does not notify it, so a reminder
added for five minutes' time could be ignored while the daemon sleeps until tomorrow.

**Two sources of truth.** The `notes` table already stores `due_at` and `status` and is
already durable. A persistent job store duplicates that, and duplicated state drifts —
a job present with no note, a note marked scheduled with no job.

## Decision

**The `notes` table is the only durable record of what still needs to happen.**
APScheduler runs with its default in-memory job store and is treated as a scheduling
*engine*, not a database.

- On start, the daemon rebuilds every pending reminder from SQLite.
- Every 30 seconds it re-reads SQLite, picking up anything added by another process.
- `replace_existing=True` on each job id (`note-<id>`) makes re-syncing idempotent, so a
  reminder can never be double-booked.

Durability comes from the notes table, which was already durable. Killing the daemon and
restarting it reconstructs the identical schedule.

## Overdue reminders

A resolved time can already be in the past — "call mum this morning" typed at 6pm
resolves to 09:00 *today* (see SPEC known limits). The daemon may also be started long
after a reminder was due.

- Due within the last **24 hours** → fires immediately, labelled *(overdue)*.
- Older than that → marked `missed`, no notification.

The second rule exists so that starting the daemon after a week away does not produce
fifty stale toasts at once. A missed reminder is still visible in `axon list`.

## Consequences

- The daemon is stateless. Its entire schedule is derived, so there is nothing to
  migrate, repair, or get out of step.
- A reminder added while the daemon runs fires up to `SYNC_SECONDS` (30s) late in the
  worst case. Acceptable for a personal reminder; if it ever isn't, `axon add` could
  signal the daemon directly.
- Job functions are bound methods rather than importable module-level functions, which
  a persistent store would have required for serialisation. This keeps the code simpler.
- **The daemon must never open memory (Mem0/Qdrant).** Local Qdrant permits a single
  process, so a daemon holding it would break every `axon add` and `axon recall` for as
  long as it ran. The daemon touches SQLite only. See ADR-0002.
