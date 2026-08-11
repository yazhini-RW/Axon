"""The WhatsApp hand: draft locally (safe), real Cloud API send only on approval
(risky). See docs/V2-PLAN.md Step 17.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from axon.config import Settings
from axon.hands import pick_hand
from axon.hands.noop import NoopHand
from axon.hands.whatsapp import WhatsAppHand, looks_like_a_whatsapp_note
from axon.models import ApprovalOutcome, Classification, ClassifiedNote, MessageDraft, NoteKind


def _note(text: str, note_id: int = 1) -> ClassifiedNote:
    return ClassifiedNote(
        note_id=note_id, text=text,
        classification=Classification(kind=NoteKind.TASK, risky=True, reason="test"),
    )


def _configured(settings: Settings) -> Settings:
    return replace(
        settings,
        whatsapp_token="test-token-not-a-real-one",
        whatsapp_phone_number_id="1234567890",
        whatsapp_to="919876543210",
        whatsapp_template="axon_note",
    )


# --- routing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "whatsapp me the wifi password",
        "whatsapp me that the deploy finished",
        "send me a whatsapp about the standup time",
        "wa me the address",
    ],
)
def test_recognises_whatsapp_notes(text: str) -> None:
    assert looks_like_a_whatsapp_note(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "fix the login bug",
        "buy milk at 5pm",
        # Deliberately narrow matching: "message"/"text" alone must not route here,
        # because guessing wrong sends a real message to a real phone.
        "message me the wifi password",
        "text me the address",
    ],
)
def test_does_not_misroute_ordinary_notes(text: str) -> None:
    assert looks_like_a_whatsapp_note(text) is False


def test_pick_hand_routes_whatsapp_notes(settings: Settings) -> None:
    assert isinstance(pick_hand("whatsapp me the wifi password", settings), WhatsAppHand)


def test_pick_hand_routes_everything_else_to_noop(settings: Settings) -> None:
    assert isinstance(pick_hand("fix the login bug", settings), NoopHand)


def test_naming_whatsapp_beats_researchs_broader_what_is_trigger(settings: Settings) -> None:
    """Research matches on "what is", which is far broader than naming a channel —
    an explicit "whatsapp me" should win. See pick_hand's ordering comment."""
    hand = pick_hand("whatsapp me what is the eiffel tower", settings)
    assert isinstance(hand, WhatsAppHand)


# --- prepare: drafts locally, never touches the network -----------------------------


async def test_prepare_strips_the_lead_in_phrase(settings: Settings) -> None:
    hand = WhatsAppHand(settings=_configured(settings))

    prepared = await hand.prepare(_note("whatsapp me the wifi password"))

    assert "the wifi password" in prepared.detail
    assert "whatsapp me" not in prepared.detail.lower()


async def test_prepare_never_touches_the_network(settings: Settings) -> None:
    hand = WhatsAppHand(settings=_configured(settings))

    with patch("httpx.AsyncClient") as mock_client_cls:
        await hand.prepare(_note("whatsapp me the wifi password"))
        mock_client_cls.assert_not_called()


async def test_prepare_shows_only_the_last_four_digits(settings: Settings) -> None:
    """The detail string lands in the approvals table and on the web UI. Confirming
    intent needs the last four digits, not the whole number."""
    hand = WhatsAppHand(settings=_configured(settings))

    prepared = await hand.prepare(_note("whatsapp me the wifi password"))

    assert "3210" in prepared.detail
    assert "919876543210" not in prepared.detail


async def test_prepare_works_with_nothing_configured(settings: Settings) -> None:
    """Drafting must stay free and safe even with no credentials — the gate is what
    stops a send, not a missing config."""
    hand = WhatsAppHand(settings=settings)

    prepared = await hand.prepare(_note("whatsapp me the wifi password"))

    assert "no recipient configured" in prepared.detail


# --- execute: real send, only once approved ------------------------------------------


@pytest.mark.parametrize(
    "missing_field, expected",
    [
        ("whatsapp_token", "WHATSAPP_TOKEN"),
        ("whatsapp_phone_number_id", "WHATSAPP_PHONE_NUMBER_ID"),
        ("whatsapp_to", "WHATSAPP_TO"),
    ],
)
async def test_execute_without_credentials_raises_clearly(
    settings: Settings, missing_field: str, expected: str
) -> None:
    configured = replace(_configured(settings), **{missing_field: None})
    hand = WhatsAppHand(settings=configured)
    outcome = ApprovalOutcome(note=_note("whatsapp me the wifi password"), approved=True)

    with pytest.raises(RuntimeError, match=expected):
        await hand.execute(outcome)


def _mock_client(status_code: int = 200, text: str = "{}"):
    response = MagicMock()
    response.is_error = status_code >= 400
    response.status_code = status_code
    response.text = text
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


async def test_execute_sends_the_drafted_message_as_a_template(settings: Settings) -> None:
    """Doesn't hit Meta's real API (that would send a real WhatsApp from a test suite) —
    patches httpx.AsyncClient and asserts the URL, auth header and body Axon builds,
    which is the part Axon controls."""
    hand = WhatsAppHand(settings=_configured(settings))
    outcome = ApprovalOutcome(note=_note("whatsapp me the wifi password"), approved=True)
    client = _mock_client()

    with patch("httpx.AsyncClient", return_value=client):
        await hand.execute(outcome)

    client.post.assert_called_once()
    args, kwargs = client.post.call_args
    assert args[0].endswith("/1234567890/messages")
    assert kwargs["headers"]["Authorization"] == "Bearer test-token-not-a-real-one"

    body = kwargs["json"]
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "919876543210"
    # Always a template, never free-form text: Axon initiates, which is always outside
    # the 24-hour window WhatsApp allows free-form replies in.
    assert body["type"] == "template"
    assert body["template"]["name"] == "axon_note"
    assert body["template"]["components"][0]["parameters"][0]["text"] == "the wifi password"


async def test_execute_strips_newlines_from_the_message(settings: Settings) -> None:
    """Real bug, found live: a template parameter containing a newline is rejected
    outright by Meta (error 132018, "Param text cannot have new-line/tab characters or
    more than 4 consecutive spaces") -- an edited multi-line draft failed every 30s
    retry for ~10 minutes with the failure invisible everywhere before this existed."""
    hand = WhatsAppHand(settings=_configured(settings))
    outcome = ApprovalOutcome(
        note=_note("whatsapp me the wifi password"), approved=True,
        draft=MessageDraft(body="the wifi password\nis on the whiteboard"),
    )
    client = _mock_client()

    with patch("httpx.AsyncClient", return_value=client):
        await hand.execute(outcome)

    sent_text = client.post.call_args[1]["json"]["template"]["components"][0]["parameters"][0]["text"]
    assert "\n" not in sent_text
    assert sent_text == "the wifi password is on the whiteboard"


async def test_execute_collapses_long_runs_of_spaces(settings: Settings) -> None:
    """Same Meta constraint, the other half: 4+ consecutive spaces are also rejected."""
    hand = WhatsAppHand(settings=_configured(settings))
    outcome = ApprovalOutcome(
        note=_note("whatsapp me the wifi password"), approved=True,
        draft=MessageDraft(body="the wifi password" + " " * 8 + "is on the whiteboard"),
    )
    client = _mock_client()

    with patch("httpx.AsyncClient", return_value=client):
        await hand.execute(outcome)

    sent_text = client.post.call_args[1]["json"]["template"]["components"][0]["parameters"][0]["text"]
    assert "    " not in sent_text


async def test_execute_surfaces_metas_error_body(settings: Settings) -> None:
    """Meta's errors are specific and worth showing ("template does not exist",
    "recipient not in allowed list") — a bare status code sends you to the dashboard
    guessing."""
    hand = WhatsAppHand(settings=_configured(settings))
    outcome = ApprovalOutcome(note=_note("whatsapp me the wifi password"), approved=True)
    client = _mock_client(status_code=400, text='{"error":{"message":"template not found"}}')

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(RuntimeError, match="template not found"):
            await hand.execute(outcome)


async def test_execute_never_puts_the_token_in_the_error(settings: Settings) -> None:
    """The token rides in the request headers; Meta never echoes it back. Guard against
    a future refactor that starts including the request in the raised message."""
    hand = WhatsAppHand(settings=_configured(settings))
    outcome = ApprovalOutcome(note=_note("whatsapp me the wifi password"), approved=True)
    client = _mock_client(status_code=401, text='{"error":{"message":"expired"}}')

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(RuntimeError) as caught:
            await hand.execute(outcome)

    assert "test-token-not-a-real-one" not in str(caught.value)
