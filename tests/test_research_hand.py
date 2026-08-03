"""The research hand: read-only lookup, no risky half. See docs/V2-PLAN.md Step 16."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from axon.config import Settings
from axon.hands import pick_hand
from axon.hands.noop import NoopHand
from axon.hands.research import ResearchHand, looks_like_a_research_note
from axon.models import ApprovalOutcome, Classification, ClassifiedNote, NoteKind


def _note(text: str, note_id: int = 1) -> ClassifiedNote:
    return ClassifiedNote(
        note_id=note_id, text=text,
        classification=Classification(kind=NoteKind.TASK, risky=False, reason="test"),
    )


def _mock_client(json_body: dict):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_body)
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


# --- routing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "research quantum computing",
        "look up the population of iceland",
        "find out when python 4 releases",
        "what is a black hole",
        "who is the ceo of anthropic",
        "summarize the theory of relativity",
    ],
)
def test_recognises_research_notes(text: str) -> None:
    assert looks_like_a_research_note(text) is True


@pytest.mark.parametrize("text", ["fix the login bug", "buy milk at 5pm"])
def test_does_not_misroute_ordinary_notes(text: str) -> None:
    assert looks_like_a_research_note(text) is False


def test_pick_hand_routes_research_notes_to_research_hand(settings: Settings) -> None:
    assert isinstance(pick_hand("research quantum computing", settings), ResearchHand)


def test_pick_hand_routes_everything_else_to_noop(settings: Settings) -> None:
    assert isinstance(pick_hand("fix the login bug", settings), NoopHand)


# --- prepare: read-only, does the whole job ------------------------------------------


async def test_prepare_returns_the_abstract_when_found() -> None:
    hand = ResearchHand()
    note = _note("what is python")

    with patch(
        "httpx.AsyncClient",
        return_value=_mock_client({
            "Answer": "", "AbstractText": "Python is a programming language.",
            "AbstractSource": "Wikipedia", "Definition": "",
        }),
    ):
        prepared = await hand.prepare(note)

    assert "Python is a programming language" in prepared.detail
    assert "Wikipedia" in prepared.detail


async def test_prepare_falls_back_to_definition_when_no_abstract() -> None:
    hand = ResearchHand()
    note = _note("what is a synonym")

    with patch(
        "httpx.AsyncClient",
        return_value=_mock_client({
            "Answer": "", "AbstractText": "", "Definition": "A word with the same meaning.",
            "DefinitionSource": "Wiktionary",
        }),
    ):
        prepared = await hand.prepare(note)

    assert "A word with the same meaning" in prepared.detail


async def test_prepare_is_honest_when_nothing_is_found() -> None:
    """Confirmed against the real API: obscure/current-event queries come back with
    every field empty. Must say so plainly, not pretend or crash."""
    hand = ResearchHand()
    note = _note("research some very obscure nonsense topic")

    with patch(
        "httpx.AsyncClient",
        return_value=_mock_client({"Answer": "", "AbstractText": "", "Definition": ""}),
    ):
        prepared = await hand.prepare(note)

    assert "no quick answer found" in prepared.detail


async def test_prepare_strips_the_trigger_phrase_from_the_query() -> None:
    hand = ResearchHand()
    note = _note("what is python")

    captured_params = {}

    def capture_get(url, params=None, timeout=None):
        captured_params.update(params or {})
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"Answer": "", "AbstractText": "", "Definition": ""})
        return response

    client = AsyncMock()
    client.get = AsyncMock(side_effect=capture_get)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    with patch("httpx.AsyncClient", return_value=client):
        await hand.prepare(note)

    assert captured_params["q"] == "python"


# --- execute: deliberate no-op, nothing risky to defer -------------------------------


async def test_execute_is_always_a_noop() -> None:
    hand = ResearchHand()
    note = _note("research quantum computing")
    outcome = ApprovalOutcome(note=note, approved=True)

    result = await hand.execute(outcome)

    assert result is None
