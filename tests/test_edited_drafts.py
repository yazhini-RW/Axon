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
from axon.models import MessageDraft, NoteStatus, utcnow


def _configured(settings: Settings) -> Settings:
    return replace(settings, email_address="me@example.com", email_app_password="pw")


async def _paused_email(
    settings: Settings, text: str = "email bob@example.com about the roadmap"
) -> int:
    """A real email note, really paused at the gate, with a matching approvals row --
    same pattern as test_scheduled_approvals.py's _paused, but through EmailHand so
    prepare() actually produces a draft to edit."""
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, text)
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
        result = service.fire_scheduled_approvals(settings=settings)

    assert len(result.fired) == 1
    message = send.call_args[0][0]
    assert message["Subject"] == "Edited subject"
    assert "Edited body text." in message.get_content()


# --- the double-notification fix: a scheduled/sent message must not also re-fire as a
# generic reminder, but a PLAIN reminder with no sending hand attached must still fire
# normally. Both directions are tested -- the second one is the near-miss caught before
# it shipped: checking due_at alone (with no hand-sent check) would have silently
# broken every ordinary "buy milk at 5pm" reminder.


def test_a_sent_timed_message_is_marked_done_not_classified(settings: Settings) -> None:
    """Prevents the double-notification bug: if this stayed CLASSIFIED, the reminder
    daemon's pending_reminders() query would later pick the note back up (kind ==
    REMINDER, due_at set, status CLASSIFIED) and fire a second, generic "this is due"
    notification on top of the email that already went out."""
    settings = _configured(settings)
    approval_id = asyncio.run(_paused_email(settings, "email bob@example.com tomorrow's meeting"))

    with patch.object(EmailHand, "_send"):
        service.resolve_approval(approval_id, True, settings=settings)

    with repo.open_db(settings) as conn:
        note = repo.get_approval(conn, approval_id)
        stored = repo.get_note(conn, note.note_id)
        assert stored.status is NoteStatus.DONE
        assert stored.due_at is not None, "the due_at itself must survive -- only the status changes"


def test_a_plain_reminder_with_no_sending_hand_still_fires_normally(settings: Settings) -> None:
    """The near-miss: this note has due_at set too, but NoopHand attaches no draft --
    its entire purpose IS the reminder notification, so it must stay CLASSIFIED so the
    daemon's pending_reminders() query still picks it up and actually fires later.

    "review the PR this evening", not "buy milk at 5pm": "buy" is a genuinely risky
    verb (spending money) in the classifier, so that example pauses for approval same
    as it always has -- picking a non-risky reminder here isn't dodging that, it's
    testing the actual "completes immediately" path this fix touches."""
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, "review the PR this evening")
        persist = service._persist_into(conn)
        capture = asyncio.run(run_capture(note.id, note.text, persist, settings=settings))

        assert capture.completed is not None, "a plain reminder must not pause at a gate"
        stored = repo.get_note(conn, note.id)
        assert stored.status is NoteStatus.CLASSIFIED
        assert stored.due_at is not None


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
