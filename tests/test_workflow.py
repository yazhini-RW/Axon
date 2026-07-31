"""The brain running as a real Microsoft Agent Framework workflow.

classify -> remember -> gate -> persist. Non-risky notes flow straight through; risky
ones pause at the gate. See docs/adr/0003-human-approval-checkpoint.md.
"""

from __future__ import annotations

import pytest

from axon.brain.workflow import (
    CHECKPOINT_TYPES,
    PendingApproval,
    resume_capture,
    run_capture,
    sweep_checkpoints,
)
from axon.config import Settings
from axon.db import repo
from axon.hands.noop import NoopHand
from axon.models import ApprovalOutcome, ClassifiedNote, NoteKind, NoteStatus, PreparedNote


def _persist_into(conn) -> tuple[callable, list[ApprovalOutcome]]:
    seen: list[ApprovalOutcome] = []

    def persist(outcome: ApprovalOutcome) -> None:
        seen.append(outcome)
        status = NoteStatus.CLASSIFIED if outcome.approved else NoteStatus.BLOCKED
        repo.apply_classification(
            conn, outcome.note.note_id, outcome.note.classification.kind,
            outcome.note.classification.due_at, status=status,
        )

    return persist, seen


# --- the non-risky path: straight through --------------------------------------------


async def test_a_harmless_reminder_completes_without_pausing(settings: Settings) -> None:
    seen: list[ApprovalOutcome] = []
    capture = await run_capture(1, "review the PR this evening", seen.append, settings=settings)

    assert capture.pending is None
    assert capture.completed is not None
    assert capture.completed.approved is True
    assert capture.completed.note.classification.kind is NoteKind.REMINDER


async def test_a_task_reaches_the_database(settings: Settings) -> None:
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "fix the login bug")
        persist, seen = _persist_into(conn)

        await run_capture(note.id, note.text, persist, settings=settings)

        assert len(seen) == 1
        stored = repo.get_note(conn, note.id)
        assert stored.kind is NoteKind.TASK
        assert stored.status is NoteStatus.CLASSIFIED


async def test_a_fact_gets_no_due_date_in_the_database(settings: Settings) -> None:
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "the office wifi password is on the whiteboard")
        persist, _ = _persist_into(conn)

        await run_capture(note.id, note.text, persist, settings=settings)

        stored = repo.get_note(conn, note.id)
        assert stored.kind is NoteKind.FACT
        assert stored.due_at is None


# --- the risky path: pause, resume, complete ------------------------------------------


async def test_a_risky_note_pauses_instead_of_completing(settings: Settings) -> None:
    capture = await run_capture(
        1, "push the axon repo to github", lambda _o: None, settings=settings
    )

    assert capture.completed is None
    assert capture.pending is not None
    assert capture.pending.request.action == "push"
    assert capture.pending.request.note.classification.risky is True


async def test_a_paused_note_still_carries_its_classification(settings: Settings) -> None:
    """A real bug from manual testing: `axon list` showed a paused note as
    "unclassified" because PersistExecutor never runs while paused. The classification
    itself is known immediately — only *acting on it* is on hold. The CLI reads this
    off `pending.request.note`, not off a later persist() call."""
    capture = await run_capture(
        1, "push the axon repo to github", lambda _o: None, settings=settings
    )
    assert capture.pending.request.note.classification.kind is NoteKind.TASK


async def test_approving_resumes_and_completes(settings: Settings) -> None:
    """Risky but not GitHub-shaped, deliberately — this exercises NoopHand's path
    through the gate, not GitHubHand's real push. See tests/test_github_hand.py."""
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "send the invoice to the client")
        persist, seen = _persist_into(conn)

        capture = await run_capture(note.id, note.text, persist, settings=settings)
        assert capture.pending is not None
        assert seen == [], "must not execute before approval"

        resumed = await resume_capture(capture.pending, True, persist, settings=settings)

        assert resumed.completed is not None
        assert resumed.completed.approved is True
        assert len(seen) == 1

        stored = repo.get_note(conn, note.id)
        assert stored.status is NoteStatus.CLASSIFIED


async def test_rejecting_resumes_and_blocks(settings: Settings) -> None:
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "send the invoice to the client")
        persist, seen = _persist_into(conn)

        capture = await run_capture(note.id, note.text, persist, settings=settings)
        resumed = await resume_capture(capture.pending, False, persist, settings=settings)

        assert resumed.completed.approved is False
        assert seen[0].approved is False

        stored = repo.get_note(conn, note.id)
        assert stored.status is NoteStatus.BLOCKED


async def test_resuming_works_with_a_brand_new_workflow_instance(settings: Settings) -> None:
    """The real test: does this survive looking like a different process?

    Nothing here shares state with the run that paused except what settings.checkpoint_dir
    holds on disk. See docs/adr/0001-microsoft-agent-framework.md.
    """
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "buy milk at 5pm")
        persist, _ = _persist_into(conn)
        capture = await run_capture(note.id, note.text, persist, settings=settings)

    pending = PendingApproval(
        note_id=capture.pending.note_id,
        request_id=capture.pending.request_id,
        checkpoint_id=capture.pending.checkpoint_id,
        # deliberately no `request` — a resume reconstructed from SQLite only ever
        # has the ids, never the original in-memory object
    )

    with repo.open_db(settings) as conn:
        persist2, seen2 = _persist_into(conn)
        resumed = await resume_capture(pending, True, persist2, settings=settings)

        assert resumed.completed is not None
        assert len(seen2) == 1
        assert repo.get_note(conn, note.id).status is NoteStatus.CLASSIFIED


async def test_a_second_pending_note_does_not_confuse_the_first(settings: Settings) -> None:
    """Guards the fix over the spike's shortcut of 'just use the latest checkpoint'."""
    first = await run_capture(1, "buy milk at 5pm", lambda _o: None, settings=settings)
    second = await run_capture(2, "push the axon repo to github", lambda _o: None, settings=settings)

    assert first.pending.checkpoint_id != second.pending.checkpoint_id
    assert first.pending.request.note.note_id == 1
    assert second.pending.request.note.note_id == 2


# --- checkpoint hygiene ----------------------------------------------------------------


async def test_completed_runs_leave_no_checkpoints_once_swept(settings: Settings) -> None:
    """SPEC known limit from Step 2: every run wrote a checkpoint nothing ever deleted."""
    await run_capture(1, "fix the login bug", lambda _o: None, settings=settings)  # never pauses

    deleted = await sweep_checkpoints(settings, keep_ids=set())
    assert deleted >= 1

    remaining = await sweep_checkpoints(settings, keep_ids=set())
    assert remaining == 0, "nothing left to sweep the second time"


async def test_a_pending_approvals_checkpoint_survives_the_sweep(settings: Settings) -> None:
    capture = await run_capture(
        1, "send the invoice to the client", lambda _o: None, settings=settings
    )
    keep = {capture.pending.checkpoint_id}

    await sweep_checkpoints(settings, keep_ids=keep)

    # if the sweep had deleted it, this would raise "no checkpoint on disk holds request"
    resumed = await resume_capture(capture.pending, True, lambda _o: None, settings=settings)
    assert resumed.completed is not None


# --- the Hand interface (Step 7) ------------------------------------------------------


class _RecordingHand:
    """A hand that notices whether prepare/execute actually ran, without doing anything."""

    def __init__(self) -> None:
        self.prepared: list[ClassifiedNote] = []
        self.executed: list[ApprovalOutcome] = []

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        self.prepared.append(note)
        return PreparedNote(note=note, detail="recorded")

    async def execute(self, outcome: ApprovalOutcome) -> None:
        self.executed.append(outcome)


async def test_noop_hand_leaves_the_non_risky_path_unchanged(settings: Settings) -> None:
    """With the default NoopHand, V2's graph must behave exactly like V1's."""
    capture = await run_capture(1, "review the PR this evening", lambda _o: None, settings=settings)

    assert capture.completed is not None
    assert capture.completed.approved is True
    assert capture.completed.note.classification.kind is NoteKind.REMINDER


async def test_prepare_always_runs_even_for_non_risky_notes(settings: Settings) -> None:
    hand = _RecordingHand()
    await run_capture(1, "fix the login bug", lambda _o: None, hand_resolver=lambda _t: hand, settings=settings)

    assert len(hand.prepared) == 1
    assert hand.prepared[0].text == "fix the login bug"


async def test_execute_runs_on_the_non_risky_path_without_a_gate_pause(settings: Settings) -> None:
    hand = _RecordingHand()
    capture = await run_capture(1, "fix the login bug", lambda _o: None, hand_resolver=lambda _t: hand, settings=settings)

    assert capture.completed is not None
    assert len(hand.executed) == 1
    assert hand.executed[0].approved is True


async def test_execute_does_not_run_until_approval(settings: Settings) -> None:
    hand = _RecordingHand()
    capture = await run_capture(
        1, "push the axon repo to github", lambda _o: None, hand_resolver=lambda _t: hand, settings=settings
    )

    assert capture.pending is not None
    assert hand.prepared, "prepare is safe, must run before the gate"
    assert not hand.executed, "execute is risky, must wait for approval"

    await resume_capture(capture.pending, True, lambda _o: None, hand_resolver=lambda _t: hand, settings=settings)
    assert len(hand.executed) == 1


async def test_execute_does_not_run_on_rejection(settings: Settings) -> None:
    hand = _RecordingHand()
    capture = await run_capture(
        1, "push the axon repo to github", lambda _o: None, hand_resolver=lambda _t: hand, settings=settings
    )

    await resume_capture(capture.pending, False, lambda _o: None, hand_resolver=lambda _t: hand, settings=settings)
    assert not hand.executed, "a rejected note must never reach execute"


async def test_default_hand_is_noop(settings: Settings) -> None:
    """No hand passed in must fall back to NoopHand, not error."""
    capture = await run_capture(1, "fix the login bug", lambda _o: None, settings=settings)
    assert capture.completed is not None


def test_checkpoint_types_are_fully_qualified_and_importable() -> None:
    """A type missing here fails silently at resume time, so assert the shape now.

    Stdlib types (zoneinfo.ZoneInfo, riding along on a reminder's due_at) belong here
    too — the rule is "never __main__", not "only our own models".
    See docs/adr/0003-human-approval-checkpoint.md.
    """
    assert CHECKPOINT_TYPES, "no types registered"
    for entry in CHECKPOINT_TYPES:
        module, _, qualname = entry.partition(":")
        assert module != "__main__", f"{entry} would fail to resume — see ADR-0003"
        assert module and qualname
