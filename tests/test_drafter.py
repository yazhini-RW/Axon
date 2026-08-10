"""Step 18: turning a one-line note into the message that actually gets sent.

The behaviour that matters most here isn't the drafting itself — it's that whatever the
human approved is what leaves the machine. See MessageDraft's docstring for why that
can't be re-derived once an LLM is involved.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from axon.brain.drafter import GeminiDrafter, PlainDrafter, get_drafter
from axon.config import Settings
from axon.hands.chat import ChatHand
from axon.hands.email import SIGNATURE, EmailHand
from axon.hands.whatsapp import WhatsAppHand
from axon.models import (
    ApprovalOutcome,
    Classification,
    ClassifiedNote,
    DraftVerdict,
    MessageDraft,
    NoteKind,
)


def _note(text: str, note_id: int = 1) -> ClassifiedNote:
    return ClassifiedNote(
        note_id=note_id, text=text,
        classification=Classification(kind=NoteKind.TASK, risky=True, reason="test"),
    )


def _gemini_returning(subject: str, body: str) -> GeminiDrafter:
    """A GeminiDrafter whose client is mocked — no key, no network, no quota."""
    drafter = GeminiDrafter.__new__(GeminiDrafter)
    response = MagicMock()
    response.value = DraftVerdict(subject=subject, body=body)
    client = AsyncMock()
    client.get_response = AsyncMock(return_value=response)
    drafter._client = client
    return drafter


# --- picking a drafter ---------------------------------------------------------------


def test_no_key_means_the_free_plain_drafter(settings: Settings) -> None:
    assert isinstance(get_drafter(settings), PlainDrafter)


def test_a_key_means_gemini(settings: Settings) -> None:
    assert isinstance(get_drafter(replace(settings, gemini_api_key="k")), GeminiDrafter)


# --- the free path is unchanged --------------------------------------------------------


async def test_plain_drafter_returns_the_hands_own_draft(settings: Settings) -> None:
    """Whatever the hand worked out deterministically, verbatim — Step 18 must not
    change behaviour for anyone without a key."""
    fallback = MessageDraft(subject="a subject", body="a body")

    result = await PlainDrafter().draft("some note", "email", fallback)

    assert result == fallback


async def test_email_prepare_without_a_key_matches_pre_step_18_behaviour(
    settings: Settings,
) -> None:
    prepared = await EmailHand(settings=settings).prepare(
        _note("email bob@example.com about the roadmap")
    )

    assert prepared.draft is not None
    assert prepared.draft.body == "email bob@example.com about the roadmap" + SIGNATURE


# --- gemini drafting -------------------------------------------------------------------


async def test_gemini_draft_replaces_the_plain_one(settings: Settings) -> None:
    drafter = _gemini_returning("Roadmap review", "Can we go over the roadmap?")

    result = await drafter.draft(
        "email bob@example.com about the roadmap",
        "email",
        MessageDraft(subject="about the roadmap", body="email bob@example.com about the roadmap"),
    )

    assert result.subject == "Roadmap review"
    assert "Can we go over the roadmap?" in result.body
    # The instruction phrasing is gone -- that's the whole point of Step 18.
    assert "email bob@example.com" not in result.body


async def test_email_greeting_and_sign_off_are_assembled_not_generated(
    settings: Settings,
) -> None:
    """A model asked for a greeting "on its own line" returned
    "Hi,I'm writing about...Thanks," with the newlines silently dropped. Structure that
    has to be right every time is built in code, not requested in a prompt."""
    drafter = _gemini_returning("Roadmap review", "Can we go over the roadmap?")

    result = await drafter.draft("note", "email", MessageDraft(subject="s", body="b"))

    assert result.body == "Hi,\n\nCan we go over the roadmap?\n\nThanks,"


async def test_chat_channel_never_gets_a_subject(settings: Settings) -> None:
    """A model may return one anyway; WhatsApp and Slack have nowhere to put it."""
    drafter = _gemini_returning("Some Subject", "Standup moved to 10.")

    result = await drafter.draft("tell the team standup moved to 10", "chat",
                                 MessageDraft(body="standup moved to 10"))

    assert result.subject is None
    assert result.body == "Standup moved to 10."


@pytest.mark.parametrize("body", ["", "   ", "\n"])
async def test_an_empty_generation_falls_back(settings: Settings, body: str) -> None:
    """Better the plain draft than an empty message."""
    fallback = MessageDraft(subject="s", body="the original note")
    drafter = _gemini_returning("subject", body)

    assert await drafter.draft("note", "email", fallback) == fallback


async def test_a_failing_model_falls_back_instead_of_raising(settings: Settings) -> None:
    """prepare() is the half that must always succeed and never cost anything. A quota
    error or network blip should leave a working approval gate, not an exception."""
    drafter = GeminiDrafter.__new__(GeminiDrafter)
    client = AsyncMock()
    client.get_response = AsyncMock(side_effect=RuntimeError("429 quota exceeded"))
    drafter._client = client
    fallback = MessageDraft(subject="s", body="the original note")

    assert await drafter.draft("note", "email", fallback) == fallback


# --- what was approved is what gets sent -----------------------------------------------


async def test_email_execute_sends_the_approved_draft_not_a_fresh_one(
    settings: Settings,
) -> None:
    """The regression this whole design exists to prevent: re-deriving at execute time
    would send a *different* message than the one on screen, because a second call to a
    model returns something else."""
    configured = replace(settings, email_address="me@example.com", email_app_password="pw")
    hand = EmailHand(settings=configured)
    approved = MessageDraft(subject="Roadmap review", body="Hi,\n\nThe approved text.")
    outcome = ApprovalOutcome(
        note=_note("email bob@example.com about the roadmap"), approved=True, draft=approved
    )

    with patch.object(hand, "_send") as send:
        await hand.execute(outcome)

    message = send.call_args[0][0]
    assert message["Subject"] == "Roadmap review"
    assert "The approved text." in message.get_content()
    # Not the raw note, which is what a re-derivation would have produced.
    assert "email bob@example.com about the roadmap" not in message.get_content()


async def test_email_execute_still_works_for_an_approval_with_no_draft(
    settings: Settings,
) -> None:
    """Approvals created before Step 18 are still sitting in the database with no draft
    attached; resuming one must not crash."""
    configured = replace(settings, email_address="me@example.com", email_app_password="pw")
    hand = EmailHand(settings=configured)
    outcome = ApprovalOutcome(note=_note("email bob@example.com about the roadmap"), approved=True)

    with patch.object(hand, "_send") as send:
        await hand.execute(outcome)

    body = send.call_args[0][0].get_content()
    assert "email bob@example.com about the roadmap" in body
    assert "Sent by Axon on your behalf" in body


async def test_whatsapp_execute_sends_the_approved_draft(settings: Settings) -> None:
    configured = replace(
        settings, whatsapp_token="t", whatsapp_phone_number_id="1", whatsapp_to="919876543210"
    )
    hand = WhatsAppHand(settings=configured)
    outcome = ApprovalOutcome(
        note=_note("whatsapp me the wifi password"), approved=True,
        draft=MessageDraft(body="The wifi password is on the whiteboard."),
    )

    response = MagicMock(is_error=False, status_code=200, text="{}")
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    with patch("httpx.AsyncClient", return_value=client):
        await hand.execute(outcome)

    params = client.post.call_args[1]["json"]["template"]["components"][0]["parameters"]
    assert params[0]["text"] == "The wifi password is on the whiteboard."


async def test_chat_execute_sends_the_approved_draft(settings: Settings) -> None:
    configured = replace(settings, chat_webhook_url="https://hooks.example.com/abc")
    hand = ChatHand(settings=configured)
    outcome = ApprovalOutcome(
        note=_note("tell the team standup moved to 10"), approved=True,
        draft=MessageDraft(body="Standup has moved to 10am."),
    )

    response = MagicMock()
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    with patch("httpx.AsyncClient", return_value=client):
        await hand.execute(outcome)

    assert client.post.call_args[1]["json"] == {"text": "Standup has moved to 10am."}


# --- the draft survives the checkpoint ---------------------------------------------------


def test_message_draft_is_registered_for_checkpoint_loading() -> None:
    """Nested models need registering in their own right. A missing entry doesn't error
    -- list_checkpoints() just comes back empty with a logged warning, so a paused note
    becomes unresumable. Exactly how NoteKind bit in Step 5. See ADR-0003."""
    from axon.brain.workflow import CHECKPOINT_TYPES

    assert "axon.models:MessageDraft" in CHECKPOINT_TYPES
