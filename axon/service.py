"""What Axon actually does, with no CLI or HTTP baked in.

Step 10: the CLI and the FastAPI backend must expose the *same* operations, not two
reimplementations of them that quietly drift apart. This module is the one place that
knows how to add/list/recall/approve — axon/cli.py and axon/web/api.py each just format
these results for their own medium (rich tables vs JSON).

Every function here takes plain arguments and returns plain dataclasses — no console
output, no HTTP status codes. Errors are real exceptions (ValueError, MemoryLocked,
ApprovalNotFound, ...); callers decide how to present them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from axon.config import Settings, get_settings
from axon.db import repo
from axon.models import ApprovalOutcome, ClassifiedNote, MessageDraft, Note, NoteStatus, utcnow
from axon.memory.store import MemoryLocked, Recollection

__all__ = [
    "MemoryLocked",
    "ApprovalNotFound",
    "ApprovalAlreadyResolved",
    "ApprovalExecutionFailed",
    "AddResult",
    "RecallResult",
    "ResolveResult",
    "DoctorInfo",
    "add_note",
    "list_notes",
    "recall",
    "list_approvals",
    "resolve_approval",
    "resolve_push_approval",
    "fire_scheduled_approvals",
    "doctor_info",
]


class ApprovalNotFound(LookupError):
    """No approval exists with this id."""


class ApprovalAlreadyResolved(RuntimeError):
    """This approval was already approved or rejected."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"already {status}")


class ApprovalExecutionFailed(RuntimeError):
    """A hand's execute() raised — e.g. GitHubHand.execute() with no GITHUB_USERNAME
    configured. The approval is deliberately left `pending` in the database (nothing
    in resolve_approval below runs after the exception), so re-approving after fixing
    the problem retries cleanly. See Step 9's GitHubHand.execute() docstring."""


@dataclass
class AddResult:
    """What happened when a note was captured. Exactly one of completed/pending is set,
    same shape as axon.brain.workflow.CaptureResult — this just adds the note and the
    approval id the CLI/API need afterwards."""

    note: Note
    completed: ApprovalOutcome | None = None
    pending_approval_id: int | None = None
    pending_action: str | None = None
    pending_detail: str = ""
    pending_push_url: str | None = None


@dataclass
class RecallResult:
    """Recall's own "is this even related" judgement, computed once here so the CLI
    and the API show the same verdict instead of each re-deriving it."""

    hits: list[Recollection]
    shown: list[Recollection]
    unsure: bool


@dataclass
class ResolveResult:
    approval_id: int
    approved: bool
    action: str
    note_text: str
    paused_again: bool  # not reachable in V1/V2 (one gate); kept for forward compat
    # Step 19. Set when this call scheduled rather than sent -- the human's "yes" is
    # recorded, but no hand has run yet. None means "already executed", same as before
    # this field existed.
    scheduled_for: datetime | None = None


@dataclass
class DoctorInfo:
    brain_mode: str
    embed_model: str
    data_dir: str
    db_path: str
    schema_version: int
    notes_total: int
    approvals_pending: int
    memory_ready: bool
    vector_dir: str


def _persist_into(conn):
    def persist(outcome: ApprovalOutcome) -> None:
        result = outcome.note
        classification = result.classification
        if not outcome.approved:
            status = NoteStatus.BLOCKED
        elif classification.due_at is not None and outcome.draft is not None:
            # Step 20 fix, narrowly scoped: outcome.draft is only ever set by a
            # message-sending hand (email/WhatsApp/chat -- see MessageDraft's
            # docstring), never by NoopHand. That distinction matters here: a PLAIN
            # reminder like "buy milk at 5pm" has due_at set too, but its entire
            # purpose IS the reminder notification -- it must stay CLASSIFIED/
            # SCHEDULED so pending_reminders() picks it up and actually fires at 5pm.
            # Checking due_at alone would have silently broken that (caught before
            # shipping, not after). Only when a real send already happened is the
            # due_at-driven notification now redundant noise on top of a message that
            # already went out -- DONE is what fire() itself sets a genuine reminder
            # to once it has fired, so this reuses that same "nothing left to remind
            # about" meaning rather than inventing a new status for the same idea.
            status = NoteStatus.DONE
        else:
            status = NoteStatus.CLASSIFIED
        repo.apply_classification(
            conn, result.note_id, classification.kind, classification.due_at,
            status=status,
        )

    return persist


def add_note(
    text: str,
    settings: Settings | None = None,
    on_captured=None,
) -> AddResult:
    """Capture a note and push it through the brain. Raises ValueError for an empty
    note, MemoryLocked if another axon command is already using memory.

    `on_captured(note)`, if given, fires right after the note is safely written down —
    before classification, which can take a moment — so a caller like the CLI can show
    immediate feedback rather than waiting in silence for the whole thing to finish.
    """
    settings = settings or get_settings()

    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, text)  # written down first, whatever happens next
        if on_captured is not None:
            on_captured(note)

        from axon.brain.workflow import run_capture, sweep_checkpoints
        from axon.memory.store import MemoryStore

        persist = _persist_into(conn)

        with MemoryStore(settings) as memory:

            def remember(result: ClassifiedNote) -> None:
                memory.remember(
                    result.text, note_id=result.note_id, kind=result.classification.kind.value
                )

            capture = asyncio.run(run_capture(note.id, note.text, persist, remember, settings=settings))

        if capture.pending:
            pending = capture.pending
            approval_id = repo.create_approval(
                conn, pending.note_id, pending.request_id, pending.checkpoint_id,
                pending.request.action,
                detail=pending.request.detail,
                project_dir=pending.request.project_dir,
                push_url=pending.request.push_url,
                draft_subject=pending.request.draft.subject if pending.request.draft else None,
                draft_body=pending.request.draft.body if pending.request.draft else None,
            )
            classification = pending.request.note.classification
            repo.apply_classification(
                conn, note.id, classification.kind, classification.due_at,
                status=NoteStatus.AWAITING_APPROVAL,
            )
            # Step 19: a 'scheduled' approval's checkpoint has to survive too -- it's
            # already been said yes to, just not run yet. Missing this here would
            # silently delete a scheduled send's ability to ever fire.
            keep = {a.checkpoint_id for a in repo.list_pending_approvals(conn)}
            keep |= {a.checkpoint_id for a in repo.list_scheduled_approvals(conn)}
            asyncio.run(sweep_checkpoints(settings, keep))

            return AddResult(
                note=note,
                pending_approval_id=approval_id,
                pending_action=pending.request.action,
                pending_detail=pending.request.detail,
                pending_push_url=pending.request.push_url,
            )

        return AddResult(note=note, completed=capture.completed)


def list_notes(limit: int = 20, settings: Settings | None = None) -> tuple[list[Note], int]:
    settings = settings or get_settings()
    with repo.open_db(settings) as conn:
        return repo.list_notes(conn, limit=limit), repo.count_notes(conn)


def list_notes_with_scheduled_for(
    limit: int = 20, settings: Settings | None = None
) -> tuple[list[tuple[Note, datetime | None]], int]:
    """Step 21: the web UI's richer notes view -- which hand a note went to (derived
    from the note text via the same predicates pick_hand uses, so it's guaranteed
    consistent with actual routing) plus when it was/is scheduled to send, persisting
    even after the send has happened. See repo.list_notes_with_scheduled_for."""
    settings = settings or get_settings()
    with repo.open_db(settings) as conn:
        return repo.list_notes_with_scheduled_for(conn, limit=limit), repo.count_notes(conn)


# Same thresholds as the CLI used before Step 10 — moved here so both callers agree.
# See docs/SPEC.md for why these are relative, not absolute (scores bunch 0.4-0.7).
_NOTHING_RELATED = 0.30
_RELATIVE_BAND = 0.15
_TOO_CLOSE_TO_CALL = 0.05


def recall(query: str, limit: int = 5, settings: Settings | None = None) -> RecallResult:
    """Raises MemoryLocked if another axon command is already using memory."""
    from axon.memory.store import MemoryStore

    settings = settings or get_settings()
    with MemoryStore(settings) as memory:
        hits = memory.recall(query, limit=limit)

    if not hits:
        return RecallResult(hits=[], shown=[], unsure=False)

    best = hits[0].score
    if best < _NOTHING_RELATED:
        return RecallResult(hits=hits, shown=[], unsure=False)

    shown = [h for h in hits if h.score >= best - _RELATIVE_BAND]
    runner_up = hits[1].score if len(hits) > 1 else 0.0
    unsure = len(shown) > 1 and (best - runner_up) < _TOO_CLOSE_TO_CALL
    return RecallResult(hits=hits, shown=shown, unsure=unsure)


def list_approvals(settings: Settings | None = None) -> list[repo.Approval]:
    settings = settings or get_settings()
    with repo.open_db(settings) as conn:
        return repo.list_pending_approvals(conn)


def resolve_approval(
    approval_id: int,
    approved: bool,
    settings: Settings | None = None,
    *,
    send_at: datetime | None = None,
    edited_draft: MessageDraft | None = None,
) -> ResolveResult:
    """Approve or reject a paused note.

    `send_at` only matters when `approved` is True (Step 19): omitted, or at/before
    now, behaves exactly as before this parameter existed -- resumes and runs the
    hand's execute() immediately. A future `send_at` instead records the "yes" and
    leaves the checkpoint paused; nothing runs until fire_scheduled_approvals() (called
    from the reminder daemon's sync loop) resumes it at that time. A reject is never
    scheduled -- there is nothing to wait for.

    `edited_draft` (Step 20) is written to the approvals row BEFORE the workflow is
    ever resumed -- including before scheduling, since a scheduled approval may not
    resume for hours, and nobody will be there to supply the edit again at that point.
    GateExecutor.resumed() reads it back out of the same row at whatever time the
    workflow actually resumes. See repo.update_draft.

    Raises ApprovalNotFound / ApprovalAlreadyResolved, or ApprovalExecutionFailed if a
    hand's execute() raised (e.g. GitHubHand.execute() with no GITHUB_USERNAME set) —
    in which case the approval is left `pending`, not marked resolved, so fixing the
    problem and re-approving retries cleanly rather than losing the local commit.
    """
    settings = settings or get_settings()
    with repo.open_db(settings) as conn:
        approval = repo.get_approval(conn, approval_id)
        if approval is None:
            raise ApprovalNotFound(str(approval_id))
        if approval.status != "pending":
            raise ApprovalAlreadyResolved(approval.status)

        if approved and edited_draft is not None:
            repo.update_draft(
                conn, approval_id, subject=edited_draft.subject, body=edited_draft.body
            )

        if approved and send_at is not None and send_at > utcnow():
            repo.schedule_approval(conn, approval_id, scheduled_for=send_at)
            repo.set_status(conn, approval.note_id, NoteStatus.SEND_SCHEDULED)
            # The checkpoint stays paused on purpose -- see list_scheduled_approvals'
            # docstring for why it must be kept alive here rather than swept.
            keep = {a.checkpoint_id for a in repo.list_pending_approvals(conn)}
            keep |= {a.checkpoint_id for a in repo.list_scheduled_approvals(conn)}
            from axon.brain.workflow import sweep_checkpoints

            asyncio.run(sweep_checkpoints(settings, keep))
            return ResolveResult(
                approval_id=approval_id, approved=True, action=approval.action,
                note_text=approval.note_text, paused_again=False, scheduled_for=send_at,
            )

        return _execute_approval(conn, approval, approved, settings)


def _execute_approval(conn, approval, approved: bool, settings: Settings) -> ResolveResult:
    """The part of resolve_approval that actually resumes the workflow -- shared by an
    immediate approve/reject and by a scheduled approval whose time has arrived
    (fire_scheduled_approvals), so both paths run the exact same code."""
    from axon.brain.workflow import PendingApproval, resume_capture, sweep_checkpoints

    persist = _persist_into(conn)
    pending = PendingApproval(
        note_id=approval.note_id,
        request_id=approval.request_id,
        checkpoint_id=approval.checkpoint_id,
    )
    try:
        capture = asyncio.run(resume_capture(pending, approved, persist, settings=settings))
    except Exception as exc:
        raise ApprovalExecutionFailed(str(exc)) from exc

    repo.resolve_approval(conn, approval.id, approved=approved)
    keep = {a.checkpoint_id for a in repo.list_pending_approvals(conn)}
    keep |= {a.checkpoint_id for a in repo.list_scheduled_approvals(conn)}
    asyncio.run(sweep_checkpoints(settings, keep))

    # Step 22: a real Claude Code build stops at the local commit -- GitHubHand.execute()
    # returns without pushing (see its docstring). The second gate lives entirely here,
    # not inside the workflow: create a fresh, separate approval for "push this" right
    # now, using whatever project_dir/push_url the completed outcome carries. push_url
    # is only ever set by GitHubHand, and the mock builder already pushed inline inside
    # execute() (nothing left to gate there) -- axon_builder == 'claude' is what tells
    # the two apart.
    outcome = capture.completed
    if (
        approved and outcome is not None and outcome.push_url and outcome.project_dir
        and settings.axon_builder == "claude"
    ):
        repo.create_push_approval(
            conn, approval.note_id,
            project_dir=outcome.project_dir, push_url=outcome.push_url,
            detail=f"Claude Code built this at {outcome.project_dir} -- look it over, "
                   "then approve to push it to GitHub.",
        )

    return ResolveResult(
        approval_id=approval.id,
        approved=approved,
        action=approval.action,
        note_text=approval.note_text,
        paused_again=capture.pending is not None,
    )


def resolve_push_approval(
    approval_id: int, approved: bool, settings: Settings | None = None
) -> ResolveResult:
    """Step 22's second gate: approve to actually run `git push`, reject to leave the
    build sitting on disk forever, never pushed. Deliberately separate from
    resolve_approval -- a 'push' row carries no real checkpoint (see _SCHEMA_V6), so
    there is nothing for resume_capture to resume; this only ever runs one git command
    or none at all.
    """
    from axon.hands.github import push_project

    settings = settings or get_settings()
    with repo.open_db(settings) as conn:
        approval = repo.get_approval(conn, approval_id)
        if approval is None:
            raise ApprovalNotFound(str(approval_id))
        if approval.kind != "push":
            raise ValueError(f"approval #{approval_id} is not a push approval")
        if approval.status != "pending":
            raise ApprovalAlreadyResolved(approval.status)

        if approved:
            try:
                asyncio.run(push_project(Path(approval.project_dir), approval.push_url))
            except Exception as exc:
                # Same contract as ApprovalExecutionFailed elsewhere: left 'pending' on
                # failure, not resolved, so a retry after fixing the problem (e.g. the
                # repo not existing yet on GitHub) works cleanly rather than losing the
                # build.
                raise ApprovalExecutionFailed(str(exc)) from exc

        repo.resolve_approval(conn, approval_id, approved=approved)
        return ResolveResult(
            approval_id=approval_id, approved=approved, action="push",
            note_text=approval.note_text, paused_again=False,
        )


@dataclass
class ScheduledFireResult:
    """What happened when the daemon checked for due scheduled sends.

    `failed` exists because a silently-swallowed failure here is worse than a manual
    approve's: a human watching the approval screen sees ApprovalExecutionFailed
    immediately, but nothing was watching a scheduled send fail at 3am. Found live, not
    hypothetically -- a real WhatsApp send failed every 30s retry for ~10 minutes with
    zero visible trace anywhere, because `continue` on ApprovalExecutionFailed used to
    be the entire error-handling story here.
    """

    fired: list[ResolveResult]
    failed: list[tuple[int, str]]  # (approval_id, error message)


def fire_scheduled_approvals(settings: Settings | None = None) -> ScheduledFireResult:
    """Run every scheduled approval whose time has arrived. Called from the reminder
    daemon's sync loop (Step 19) -- scheduled sends only happen while `axon run` is
    active, same as reminder notifications always have.

    A send that raises here behaves like ApprovalExecutionFailed always has: the row
    stays 'scheduled' (resolve_approval is never reached on that path inside
    _execute_approval), so the next sync retries it rather than losing it silently --
    but see ScheduledFireResult.failed for why "not lost" isn't the same as "visible".
    """
    settings = settings or get_settings()
    fired: list[ResolveResult] = []
    failed: list[tuple[int, str]] = []
    with repo.open_db(settings) as conn:
        due = [
            a for a in repo.list_scheduled_approvals(conn)
            if a.scheduled_for is not None and a.scheduled_for <= utcnow()
        ]
        for approval in due:
            try:
                fired.append(_execute_approval(conn, approval, True, settings))
            except ApprovalExecutionFailed as exc:
                failed.append((approval.id, str(exc)))
    return ScheduledFireResult(fired=fired, failed=failed)


def doctor_info(settings: Settings | None = None) -> DoctorInfo:
    settings = settings or get_settings()
    with repo.open_db(settings) as conn:
        total = repo.count_notes(conn)
        pending = len(repo.list_pending_approvals(conn))
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    return DoctorInfo(
        brain_mode=settings.brain_mode,
        embed_model=settings.embed_model,
        data_dir=str(settings.data_dir),
        db_path=str(settings.db_path),
        schema_version=version,
        notes_total=total,
        approvals_pending=pending,
        memory_ready=settings.vector_dir.exists(),
        vector_dir=str(settings.vector_dir),
    )
