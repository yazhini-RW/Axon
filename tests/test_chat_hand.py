"""The chat hand: draft locally (safe), real Slack/Teams webhook post only on
approval (risky). See docs/V2-PLAN.md Step 14.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from axon.config import Settings
from axon.hands import pick_hand
from axon.hands.chat import ChatHand, looks_like_a_chat_note
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
        "post in slack that the deploy finished",
        "tell the team standup moved to 10",
        "message the team about the outage",
        "notify the team that the build is fixed",
        "post an update to teams about the release",
    ],
)
def test_recognises_chat_notes(text: str) -> None:
    assert looks_like_a_chat_note(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "fix the login bug",
        "buy milk at 5pm",
        "join the team meeting at 3pm",  # "team" alone, no platform/trigger phrase
    ],
)
def test_does_not_misroute_ordinary_notes(text: str) -> None:
    assert looks_like_a_chat_note(text) is False


def test_pick_hand_routes_chat_notes_to_chat_hand(settings: Settings) -> None:
    assert isinstance(pick_hand("tell the team standup moved to 10", settings), ChatHand)


def test_pick_hand_routes_everything_else_to_noop(settings: Settings) -> None:
    assert isinstance(pick_hand("fix the login bug", settings), NoopHand)


# --- prepare: drafts locally, never touches the network -----------------------------


async def test_prepare_strips_the_lead_in_phrase(settings: Settings) -> None:
    hand = ChatHand(settings=settings)
    note = _note("tell the team standup moved to 10")

    prepared = await hand.prepare(note)

    assert "standup moved to 10" in prepared.detail
    assert "tell the team" not in prepared.detail.lower()


async def test_prepare_never_touches_the_network(settings: Settings) -> None:
    hand = ChatHand(settings=settings)
    note = _note("tell the team standup moved to 10")

    with patch("httpx.AsyncClient") as mock_client_cls:
        await hand.prepare(note)
        mock_client_cls.assert_not_called()


# --- execute: real post, only once approved ------------------------------------------


async def test_execute_without_webhook_url_raises_clearly(settings: Settings) -> None:
    """settings fixture has no chat_webhook_url configured."""
    hand = ChatHand(settings=settings)
    note = _note("tell the team standup moved to 10")
    outcome = ApprovalOutcome(note=note, approved=True)

    with pytest.raises(RuntimeError, match="CHAT_WEBHOOK_URL"):
        await hand.execute(outcome)


async def test_execute_posts_the_drafted_message(settings: Settings) -> None:
    """Doesn't hit a real Slack/Teams webhook (no free, safe way to do that in a test
    suite) - patches httpx.AsyncClient and asserts Axon posts the right URL and body,
    which is the part Axon controls. The real post path was verified manually against
    a real webhook."""
    configured = replace(settings, chat_webhook_url="https://hooks.example.com/abc123")
    hand = ChatHand(settings=configured)
    note = _note("tell the team standup moved to 10")
    outcome = ApprovalOutcome(note=note, approved=True)

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    with patch("httpx.AsyncClient", return_value=mock_client):
        await hand.execute(outcome)

    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://hooks.example.com/abc123"
    assert kwargs["json"] == {"text": "standup moved to 10"}
    mock_response.raise_for_status.assert_called_once()
