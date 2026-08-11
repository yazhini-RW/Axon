"""Step 19: approving with a future time schedules a send instead of running it now.

Follows the same pattern test_workflow.py uses to avoid the slow embedding model:
run_capture()/resume_capture() directly against a real paused checkpoint, with a
manually-inserted approvals row alongside it -- service.resolve_approval() itself never
touches memory (only service.add_note() does), so this is safe to run at full speed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from axon import service
from axon.brain.workflow import run_capture
from axon.config import Settings
from axon.db import repo
from axon.models import utcnow


async def _paused(settings: Settings, text: str = "send the invoice to the client") -> int:
    """A real risky note, really paused at the gate (NoopHand, no network needed --
    same choice test_workflow.py makes), with a matching approvals row so
    service.resolve_approval() can find it exactly the way the real add_note() flow
    would have left it."""
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, text)
        capture = await run_capture(note.id, note.text, lambda _o: None, settings=settings)
        assert capture.pending is not None, "expected this note to pause at the gate"
        return repo.create_approval(
            conn, note.id, capture.pending.request_id, capture.pending.checkpoint_id,
            capture.pending.request.action,
        )


# --- resolve_approval: now vs later ---------------------------------------------------


def test_approving_with_no_time_behaves_exactly_as_before(settings: Settings) -> None:
    approval_id = asyncio.run(_paused(settings))

    result = service.resolve_approval(approval_id, True, settings=settings)

    assert result.scheduled_for is None
    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, approval_id).status == "approved"


def test_approving_with_a_past_time_sends_immediately(settings: Settings) -> None:
    """A "later" that has already arrived is just "now" -- there is nothing to wait
    for, so this must not get stuck as forever-scheduled."""
    approval_id = asyncio.run(_paused(settings))

    result = service.resolve_approval(
        approval_id, True, settings=settings, send_at=utcnow() - timedelta(minutes=1)
    )

    assert result.scheduled_for is None
    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, approval_id).status == "approved"


def test_approving_with_a_future_time_schedules_instead_of_sending(
    settings: Settings,
) -> None:
    approval_id = asyncio.run(_paused(settings))
    send_at = utcnow() + timedelta(hours=2)

    result = service.resolve_approval(approval_id, True, settings=settings, send_at=send_at)

    assert result.scheduled_for == send_at
    with repo.open_db(settings) as conn:
        approval = repo.get_approval(conn, approval_id)
        assert approval.status == "scheduled"
        assert approval.scheduled_for == send_at


def test_a_reject_is_never_scheduled_even_with_a_time_given(settings: Settings) -> None:
    """There is nothing to wait for when the answer is no -- send_at only makes sense
    alongside approved=True."""
    approval_id = asyncio.run(_paused(settings))

    result = service.resolve_approval(
        approval_id, False, settings=settings, send_at=utcnow() + timedelta(hours=2)
    )

    assert result.scheduled_for is None
    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, approval_id).status == "rejected"


def test_a_scheduled_approval_cannot_be_approved_again(settings: Settings) -> None:
    """'scheduled' is not 'pending' -- resolve_approval must reject a second attempt
    the same way it already does for 'approved'/'rejected'."""
    approval_id = asyncio.run(_paused(settings))
    service.resolve_approval(
        approval_id, True, settings=settings, send_at=utcnow() + timedelta(hours=2)
    )

    with pytest.raises(service.ApprovalAlreadyResolved, match="scheduled"):
        service.resolve_approval(approval_id, True, settings=settings)


# --- fire_scheduled_approvals: what the daemon does at the scheduled time ------------
#
# A scheduled send only ever gets created for a genuinely future time (resolve_approval
# treats a past-or-now time as "send immediately" -- see test_approving_with_a_past_
# time_sends_immediately above). To exercise "the scheduled time has since arrived",
# these schedule for the future through the real API first, then move that same row
# into the past directly at the repo layer -- simulating time passing, not a different
# code path for creating the row.


def _schedule_then_make_it_due(settings: Settings) -> int:
    approval_id = asyncio.run(_paused(settings))
    service.resolve_approval(
        approval_id, True, settings=settings, send_at=utcnow() + timedelta(hours=2)
    )
    with repo.open_db(settings) as conn:
        repo.schedule_approval(conn, approval_id, scheduled_for=utcnow() - timedelta(seconds=1))
    return approval_id


def test_fire_scheduled_approvals_ignores_ones_not_due_yet(settings: Settings) -> None:
    approval_id = asyncio.run(_paused(settings))
    service.resolve_approval(
        approval_id, True, settings=settings, send_at=utcnow() + timedelta(hours=2)
    )

    result = service.fire_scheduled_approvals(settings=settings)

    assert result.fired == []
    assert result.failed == []
    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, approval_id).status == "scheduled"


def test_fire_scheduled_approvals_runs_a_due_one(settings: Settings) -> None:
    approval_id = _schedule_then_make_it_due(settings)

    result = service.fire_scheduled_approvals(settings=settings)

    assert len(result.fired) == 1
    assert result.fired[0].approval_id == approval_id
    assert result.failed == []
    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, approval_id).status == "approved"


def test_fire_scheduled_approvals_only_fires_each_one_once(settings: Settings) -> None:
    _schedule_then_make_it_due(settings)

    first = service.fire_scheduled_approvals(settings=settings)
    second = service.fire_scheduled_approvals(settings=settings)

    assert len(first.fired) == 1
    assert second.fired == []


def test_a_failing_send_stays_scheduled_for_the_next_attempt(settings: Settings) -> None:
    """Same contract ApprovalExecutionFailed already gives a manual approve: a hand
    that raises must not lose the request. It should still be there next sync, not
    silently dropped because nobody was watching it fail."""
    approval_id = _schedule_then_make_it_due(settings)

    import axon.brain.workflow as workflow_module

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated send failure")

    original = workflow_module.resume_capture
    workflow_module.resume_capture = boom
    try:
        result = service.fire_scheduled_approvals(settings=settings)
    finally:
        workflow_module.resume_capture = original

    assert result.fired == []
    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, approval_id).status == "scheduled"


def test_a_failing_send_is_reported_not_just_swallowed(settings: Settings) -> None:
    """The real bug this was built to catch: a scheduled send failing used to be
    invisible everywhere -- the row just silently stayed 'scheduled' forever with no
    trace. Found live: a WhatsApp send failed every 30s retry for ~10 minutes with
    nothing anywhere saying so."""
    approval_id = _schedule_then_make_it_due(settings)

    import axon.brain.workflow as workflow_module

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated send failure")

    original = workflow_module.resume_capture
    workflow_module.resume_capture = boom
    try:
        result = service.fire_scheduled_approvals(settings=settings)
    finally:
        workflow_module.resume_capture = original

    assert len(result.failed) == 1
    failed_id, error = result.failed[0]
    assert failed_id == approval_id
    assert "simulated send failure" in error
