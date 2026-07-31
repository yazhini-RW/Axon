"""GeminiBrain against a stubbed client.

No real Gemini key is available in this environment (per project rules: mock-first, add
a key later), so these test GeminiBrain's own logic — parsing the verdict, the risk
safety-net, the due-date fallback — against a fake client that returns a canned
GeminiVerdict. They do not exercise the real network call or the real base URL. See the
Limits section of docs/adr/0005-gemini-brain.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from axon.brain.gemini import GeminiBrain
from axon.models import GeminiVerdict, NoteKind


class _FakeResponse:
    def __init__(self, value: GeminiVerdict) -> None:
        self.value = value


class _FakeClient:
    """Stands in for OpenAIChatClient. Records what it was asked, returns what we tell it."""

    def __init__(self, verdict: GeminiVerdict) -> None:
        self._verdict = verdict
        self.last_messages = None
        self.last_options = None

    async def get_response(self, messages, *, options=None, **_kwargs):
        self.last_messages = messages
        self.last_options = options
        return _FakeResponse(self._verdict)


def _brain_with(verdict: GeminiVerdict) -> tuple[GeminiBrain, _FakeClient]:
    brain = GeminiBrain(api_key="unused-in-tests")
    fake = _FakeClient(verdict)
    brain._client = fake  # the only seam available without hitting the real network
    return brain, fake


# --- basic mapping ---------------------------------------------------------------------


async def test_a_fact_verdict_maps_straight_through() -> None:
    brain, _ = _brain_with(
        GeminiVerdict(kind=NoteKind.FACT, reason="nothing to do here")
    )
    result = await brain.classify("we chose Microsoft Agent Framework")

    assert result.kind is NoteKind.FACT
    assert result.due_at is None
    assert result.risky is False
    assert result.reason == "nothing to do here"


async def test_a_reminder_verdict_gets_its_time_parsed_by_our_own_extractor() -> None:
    """Gemini gives the phrase, extract_due() (Step 2's hardened parser) resolves it —
    not the LLM's own date arithmetic. See ADR-0005."""
    brain, _ = _brain_with(
        GeminiVerdict(kind=NoteKind.REMINDER, when="in 5 days", reason="a future task")
    )
    result = await brain.classify("in 5 days remind me to build project idea X")

    assert result.kind is NoteKind.REMINDER
    assert result.due_at is not None
    assert result.due_at.tzinfo is not None


# --- the safety net: risk is never trusted to the LLM alone ---------------------------


async def test_risky_is_true_if_either_gemini_or_the_keyword_check_says_so() -> None:
    """Gemini under-calling risk must not leave a genuinely risky action unguarded."""
    brain, _ = _brain_with(
        GeminiVerdict(kind=NoteKind.TASK, risky=False, reason="gemini missed this one")
    )
    result = await brain.classify("push the axon repo to github")  # "push" is a keyword

    assert result.risky is True, "the keyword floor must catch what the LLM misses"


async def test_gemini_can_also_flag_risk_the_keywords_would_miss() -> None:
    brain, _ = _brain_with(
        GeminiVerdict(kind=NoteKind.TASK, risky=True, reason="gemini spotted something subtle")
    )
    result = await brain.classify("finalise the contract with the vendor")

    assert result.risky is True


async def test_a_fact_is_never_risky_even_if_gemini_says_so() -> None:
    """Mirrors the mock brain's own rule: risk is about what Axon will *do*."""
    brain, _ = _brain_with(
        GeminiVerdict(kind=NoteKind.FACT, risky=True, reason="gemini got confused")
    )
    result = await brain.classify("the deploy key is in bitwarden")

    assert result.risky is False


# --- a reminder with no parseable time is not silently a dangling reminder -------------


async def test_a_reminder_verdict_with_no_when_becomes_a_task() -> None:
    brain, _ = _brain_with(GeminiVerdict(kind=NoteKind.REMINDER, when=None, reason="vague"))
    result = await brain.classify("someday I should reorganise my notes")

    assert result.kind is NoteKind.TASK, "a reminder with nothing to schedule is a task"


async def test_a_reminder_verdict_with_an_unparseable_when_becomes_a_task() -> None:
    brain, _ = _brain_with(
        GeminiVerdict(kind=NoteKind.REMINDER, when="whenever", reason="not a real time")
    )
    result = await brain.classify("call mum whenever")

    assert result.kind is NoteKind.TASK
    assert result.due_at is None


# --- what actually gets sent -----------------------------------------------------------


async def test_the_note_text_reaches_the_client_unmodified() -> None:
    """Also guards the Message(role, contents) trap: a bare str is an iterable of
    characters to that constructor, not one text block — it must be wrapped in a list,
    or .text comes back as the note letter-by-letter with spaces between."""
    brain, fake = _brain_with(GeminiVerdict(kind=NoteKind.FACT, reason="ok"))
    await brain.classify("a very specific note about pineapples")

    assert fake.last_messages[-1].text == "a very specific note about pineapples"


async def test_structured_output_is_requested_as_geminiverdict() -> None:
    brain, fake = _brain_with(GeminiVerdict(kind=NoteKind.FACT, reason="ok"))
    await brain.classify("anything")

    assert fake.last_options["response_format"] is GeminiVerdict


# --- construction ------------------------------------------------------------------------


def test_default_model_is_set() -> None:
    from axon.brain.gemini import DEFAULT_MODEL

    assert DEFAULT_MODEL  # non-empty; overridable per ADR-0005 if Google renames it


def test_base_url_is_gemini_not_openai() -> None:
    from axon.brain.gemini import GEMINI_BASE_URL

    assert "generativelanguage.googleapis.com" in GEMINI_BASE_URL
