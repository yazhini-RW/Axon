"""The email hand: draft locally (safe), real SMTP send only on approval (risky). See
docs/V2-PLAN.md Step 13.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from axon.config import Settings
from axon.hands import pick_hand
from axon.hands.email import EmailHand, looks_like_an_email_note
from axon.hands.noop import NoopHand
from axon.models import ApprovalOutcome, Classification, ClassifiedNote, NoteKind


def _note(text: str, note_id: int = 1) -> ClassifiedNote:
    return ClassifiedNote(
        note_id=note_id, text=text,
        classification=Classification(kind=NoteKind.TASK, risky=True, reason="test"),
    )


# --- routing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "email sarah@example.com about the invoice",
        "send an email to john.smith@company.co about tomorrow's meeting",
        "mail the report to boss@work.org",
    ],
)
def test_recognises_email_notes(text: str) -> None:
    assert looks_like_an_email_note(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "my email is private",  # no address, just mentions the word
        "sarah@example.com is my emergency contact",  # address but no email verb
        "fix the login bug",
        "buy milk at 5pm",
    ],
)
def test_does_not_misroute_ordinary_notes(text: str) -> None:
    assert looks_like_an_email_note(text) is False


def test_pick_hand_routes_email_notes_to_email_hand(settings: Settings) -> None:
    assert isinstance(pick_hand("email sarah@example.com about the invoice", settings), EmailHand)


def test_pick_hand_routes_everything_else_to_noop(settings: Settings) -> None:
    assert isinstance(pick_hand("fix the login bug", settings), NoopHand)


def test_github_routing_takes_priority_over_email(settings: Settings) -> None:
    """A note can't sensibly be both, but if wording overlapped, GitHub wins - checked
    first in pick_hand(). Documents the order, not a real conflict in practice."""
    from axon.hands.github import GitHubHand

    hand = pick_hand("build a github repo, then email sarah@example.com about it", settings)
    assert isinstance(hand, GitHubHand)


# --- prepare: drafts locally, never touches the network -----------------------------


async def test_prepare_extracts_the_recipient(settings: Settings) -> None:
    hand = EmailHand(settings=settings)
    note = _note("email sarah@example.com about the invoice")

    prepared = await hand.prepare(note)

    assert "sarah@example.com" in prepared.detail
    assert "draft ready" in prepared.detail


@pytest.mark.parametrize(
    ("text", "expected_subject"),
    [
        ("email sarah@example.com about the quarterly report", "about the quarterly report"),
        ("send an email to john@x.com about tomorrow's meeting", "about tomorrow's meeting"),
        ("mail the report to boss@x.org", "the report"),
    ],
)
def test_draft_strips_the_lead_in_phrase_from_the_subject(text: str, expected_subject: str) -> None:
    """Regression: the first version left the trigger word in the subject line -
    "email about the quarterly report" instead of "about the quarterly report"."""
    from axon.hands.email import _draft

    _recipient, subject, _body = _draft(text)
    assert subject == expected_subject


async def test_prepare_never_touches_the_network(settings: Settings) -> None:
    hand = EmailHand(settings=settings)
    note = _note("email sarah@example.com about the invoice")

    with patch("smtplib.SMTP") as mock_smtp:
        await hand.prepare(note)
        mock_smtp.assert_not_called()


# --- execute: real send, only once approved ------------------------------------------


async def test_execute_without_credentials_raises_clearly(settings: Settings) -> None:
    """settings fixture has no email_address/email_app_password configured."""
    hand = EmailHand(settings=settings)
    note = _note("email sarah@example.com about the invoice")
    outcome = ApprovalOutcome(note=note, approved=True)

    with pytest.raises(RuntimeError, match="EMAIL_ADDRESS"):
        await hand.execute(outcome)


async def test_execute_sends_with_the_right_fields(settings: Settings) -> None:
    """Doesn't hit a real SMTP server (no free, safe way to do that in a test suite) -
    patches smtplib.SMTP and asserts Axon builds the right message and calls the right
    sequence (STARTTLS then login then send), which is the part Axon controls. The
    real send path was verified manually against a real Gmail account."""
    configured = replace(
        settings, email_address="axon@example.com", email_app_password="app-password-123",
    )
    hand = EmailHand(settings=configured)
    note = _note("email sarah@example.com about the invoice")
    outcome = ApprovalOutcome(note=note, approved=True)

    mock_conn = MagicMock()
    mock_smtp_ctx = MagicMock()
    mock_smtp_ctx.__enter__.return_value = mock_conn
    with patch("smtplib.SMTP", return_value=mock_smtp_ctx) as mock_smtp:
        await hand.execute(outcome)

    mock_smtp.assert_called_once_with(configured.smtp_host, configured.smtp_port)
    mock_conn.starttls.assert_called_once()
    mock_conn.login.assert_called_once_with("axon@example.com", "app-password-123")
    mock_conn.send_message.assert_called_once()

    sent_message = mock_conn.send_message.call_args[0][0]
    assert sent_message["To"] == "sarah@example.com"
    assert sent_message["From"] == "axon@example.com"
    assert "invoice" in sent_message["Subject"]
