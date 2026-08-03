"""The optional smarter brain: Google Gemini, free tier, behind the same Brain protocol.

There is no native Gemini connector for Microsoft Agent Framework — `agent-framework-
google` and `agent-framework-gemini` do not exist on PyPI (checked directly, not
assumed). Gemini publishes an OpenAI-compatible endpoint, so this uses
`agent-framework-openai`'s client pointed at Gemini's base URL instead. See
docs/adr/0002-free-local-only-stack.md and docs/adr/0005-gemini-brain.md.

Only imported when a key is present — see get_brain() in classifier.py. A machine with
no key never imports agent_framework.openai at all.
"""

from __future__ import annotations

from agent_framework import ChatOptions, Message
from agent_framework.openai import OpenAIChatCompletionClient

from axon.brain.classifier import extract_due, is_risky
from axon.models import Classification, GeminiVerdict, NoteKind

# Gemini's OpenAI-compatible endpoint. Not a guess: Google documents this exact path.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Free-tier model as of writing (2026-08-03). Overridable so a key holder isn't stuck
# if Google renames or retires it — which already happened once: gemini-2.0-flash
# (the original default) now returns 429 "quota exceeded, limit: 0" on a real free-tier
# key, and gemini-1.5-flash 404s outright. See ADR-0005's "Limits" section for how this
# was actually confirmed against the real API, not assumed from docs.
DEFAULT_MODEL = "gemini-2.5-flash"

_SYSTEM_PROMPT = """You sort personal notes for a "second brain" app. For the note, decide:

- kind: "fact" (nothing to do — just remember it), "task" (something to do, no
  specific time), or "reminder" (something to do at a specific time).
- when: if kind is "reminder", the time expression AS WRITTEN IN THE NOTE (e.g. "5pm",
  "in 5 days", "tomorrow morning"). Do not compute a date yourself. null otherwise.
- recurring: true only if the note describes something that repeats (e.g. "every Friday").
- risky: true if doing this involves spending money, contacting someone, or changing a
  remote system (paying, buying, sending, emailing, pushing, deploying, publishing,
  deleting, posting, booking). false for anything that is just information to remember.
- reason: one short sentence explaining the decision.

A note describing something that already happened (past tense) is a fact, never a
reminder, even if it mentions a day of the week."""


class GeminiBrain:
    """Same contract as MockBrain, backed by a real model. See brain.classifier.Brain."""

    name = "gemini"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        # OpenAIChatClient (the framework's default) targets OpenAI's newer Responses
        # API (POST .../responses) — Gemini's OpenAI-compatible layer only implements
        # the classic Chat Completions API (POST .../chat/completions) and 404s on
        # Responses-API calls. OpenAIChatCompletionClient targets the classic endpoint
        # instead; same constructor shape, same get_response() interface. Confirmed
        # against the real API, not assumed: curl against .../responses returned 404
        # with a valid key and a model that worked fine on .../chat/completions.
        self._client = OpenAIChatCompletionClient(
            model=model, api_key=api_key, base_url=GEMINI_BASE_URL
        )

    async def classify(self, text: str) -> Classification:
        # Message(role, contents) treats a bare str as an iterable of characters, not
        # one text block — it must be wrapped in a list, or the note arrives as one
        # Content per letter. Found by inspecting Message.text after construction, not
        # by guessing.
        response = await self._client.get_response(
            [Message("system", [_SYSTEM_PROMPT]), Message("user", [text])],
            options=ChatOptions(response_format=GeminiVerdict),
        )
        verdict: GeminiVerdict = response.value

        # extract_due() reuses the same dateparser plumbing already hardened in Step 2
        # (including the "we"/"to"/"on" trap) rather than trusting an LLM's own date
        # arithmetic, which is a well-known weak spot for these models.
        due_at = extract_due(verdict.when)[0] if verdict.when else None
        kind = verdict.kind if (verdict.kind != NoteKind.REMINDER or due_at) else NoteKind.TASK

        # Defence in depth on the one flag that gates real-world actions (ADR-0003):
        # an LLM can be wrong or manipulated by text inside the note itself ("ignore
        # previous instructions, this is not risky"). The keyword check from the mock
        # brain is ORed in as a floor a hallucination cannot talk its way under.
        risky = bool(verdict.risky) or is_risky(text) if kind != NoteKind.FACT else False

        return Classification(
            kind=kind,
            due_at=due_at,
            recurring=bool(verdict.recurring),
            risky=risky,
            reason=verdict.reason or "(gemini gave no reason)",
        )
