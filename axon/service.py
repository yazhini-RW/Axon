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

from axon.config import Settings, get_settings
from axon.db import repo
from axon.models import ApprovalOutcome, ClassifiedNote, Note, NoteStatus
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
        status = NoteStatus.CLASSIFIED if outcome.approved else NoteStatus.BLOCKED
        repo.apply_classification(
            conn, result.note_id, result.classification.kind, result.classification.due_at,
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
            keep = {a.checkpoint_id for a in repo.list_pending_approvals(conn)}
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
    approval_id: int, approved: bool, settings: Settings | None = None
) -> ResolveResult:
    """Approve or reject a paused note.

    Raises ApprovalNotFound / ApprovalAlreadyResolved, or ApprovalExecutionFailed if a
    hand's execute() raised (e.g. GitHubHand.execute() with no GITHUB_USERNAME set) —
    in which case the approval is left `pending`, not marked resolved, so fixing the
    problem and re-approving retries cleanly rather than losing the local commit.
    """
    from axon.brain.workflow import PendingApproval, resume_capture, sweep_checkpoints

    settings = settings or get_settings()
    with repo.open_db(settings) as conn:
        approval = repo.get_approval(conn, approval_id)
        if approval is None:
            raise ApprovalNotFound(str(approval_id))
        if approval.status != "pending":
            raise ApprovalAlreadyResolved(approval.status)

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

        repo.resolve_approval(conn, approval_id, approved=approved)
        keep = {a.checkpoint_id for a in repo.list_pending_approvals(conn)}
        asyncio.run(sweep_checkpoints(settings, keep))

        return ResolveResult(
            approval_id=approval_id,
            approved=approved,
            action=approval.action,
            note_text=approval.note_text,
            paused_again=capture.pending is not None,
        )


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
