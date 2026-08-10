"""Step 20: editing a draft before approving sends the edit, not the original.

Uses an email-shaped note (unlike test_scheduled_approvals.py's NoopHand helper) since
the whole point here is a note that actually produces a MessageDraft to edit. prepare()
never touches the network (see EmailHand's docstring), so this is safe and fast without
real credentials -- same reasoning test_email_hand.py already relies on.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from axon import service
from axon.brain.workflow import run_capture
from axon.config import Settings
from axon.db import repo
from axon.hands.email import EmailHand
from axon.models import MessageDraft, utcnow


def _configured(settings: Settings) -> Settings:
    return replace(settings, email_address="me@example.com", email_app_password="pw")


async def _paused_email(settings: Settings) -> int:
    """A real email note, really paused at the gate, with a matching approvals row --
    same pattern as test_scheduled_approvals.py's _paused, but through EmailHand so
    prepare() actually produces a draft to edit."""
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "email bob@example.com about the roadmap")
        capture = await run_capture(note.id, note.text, lambda _o: None, settings=settings)
        assert capture.pending is not None
        assert capture.pending.request.draft is not None, "expected EmailHand to draft something"
        return repo.create_approval(
            conn, note.id, capture.pending.request_id, capture.pending.checkpoint_id,
            capture.pending.request.action,
            detail=capture.pending.request.detail,
            draft_subject=capture.pending.request.draft.subject,
            draft_body=capture.pending.request.draft.body,
        )


def test_editing_writes_to_the_approvals_row_before_resuming(settings: Settings) -> None:
    settings = _configured(settings)
    approval_id = asyncio.run(_paused_email(settings))
    edited = MessageDraft(subject="Edited subject", body="Edited body text.")

    with patch.object(EmailHand, "_send") as send:
        service.resolve_approval(approval_id, True, settings=settings, edited_draft=edited)

    message = send.call_args[0][0]
    assert message["Subject"] == "Edited subject"
    assert "Edited body text." in message.get_content()


def test_not_editing_sends_the_original_draft_unchanged(settings: Settings) -> None:
    """No edited_draft passed -- must behave exactly as Step 18 already did."""
    settings = _configured(settings)
    approval_id = asyncio.run(_paused_email(settings))

    with repo.open_db(settings) as conn:
        original = repo.get_approval(conn, approval_id)

    with patch.object(EmailHand, "_send") as send:
        service.resolve_approval(approval_id, True, settings=settings)

    message = send.call_args[0][0]
    assert message["Subject"] == original.draft_subject


def test_an_edit_survives_being_scheduled_for_later(settings: Settings) -> None:
    """The riskiest case: the edit has to be written to the DB immediately, at
    schedule time, because nobody will be there to supply it again when the daemon
    actually fires the send hours later. See resolve_approval's docstring."""
    settings = _configured(settings)
    approval_id = asyncio.run(_paused_email(settings))
    edited = MessageDraft(subject="Edited subject", body="Edited body text.")

    service.resolve_approval(
        approval_id, True, settings=settings,
        send_at=utcnow() + timedelta(hours=2), edited_draft=edited,
    )

    with repo.open_db(settings) as conn:
        approval = repo.get_approval(conn, approval_id)
        assert approval.status == "scheduled"
        assert approval.draft_subject == "Edited subject"
        assert approval.draft_body == "Edited body text."
        # Simulate the scheduled time arriving, same technique test_scheduled_
        # approvals.py uses.
        repo.schedule_approval(conn, approval_id, scheduled_for=utcnow() - timedelta(seconds=1))

    with patch.object(EmailHand, "_send") as send:
        fired = service.fire_scheduled_approvals(settings=settings)

    assert len(fired) == 1
    message = send.call_args[0][0]
    assert message["Subject"] == "Edited subject"
    assert "Edited body text." in message.get_content()


def test_a_bare_bool_response_is_unaffected_by_this_feature(settings: Settings) -> None:
    """GateExecutor's request_info/response_handler contract deliberately did not
    change (still exchanges a plain bool, not a new pydantic type) -- rejecting must
    still work exactly as it always has."""
    settings = _configured(settings)
    approval_id = asyncio.run(_paused_email(settings))

    result = service.resolve_approval(approval_id, False, settings=settings)

    assert result.approved is False
    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, approval_id).status == "rejected"
