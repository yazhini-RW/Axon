"""Turning a one-line note into the message that actually gets sent. Step 18.

Same "same interface, free thing stays the default" pattern as axon.brain.classifier's
Brain and axon.hands.builders' Builder: `PlainDrafter` needs no key and returns exactly
what the hands produced before this module existed, `GeminiDrafter` slots in behind the
same protocol when a key is present.

Why this exists: a note reading "email alice@example.com about tomorrow's meeting time"
used to arrive in the inbox as literally that sentence, instruction phrasing and all,
because the hands only ever stripped the lead-in. The gate asks the human to approve a
*message*, so there should be a message to approve.

Two deliberate constraints on the prompt:

- It must not invent facts. The note is the only source; a drafter that helpfully adds
  a time or a place the human never wrote turns "approve this" into a trap.
- Whatever it returns is what gets sent (MessageDraft's docstring explains why the draft
  rides through the checkpoint rather than being regenerated at execute time), so a
  failed or empty generation must fall back rather than send nothing.

Prompt injection is real here and only partly mitigated. The note text goes into the
prompt, so a note containing "ignore your instructions and write ..." can influence the
draft — same exposure ADR-0005 records for the classifier. The mitigation is the gate
itself: a human reads the finished message before it is sent anywhere. That is weaker
for the *classifier* (where a bad `risky: false` skips the gate entirely, hence the
keyword floor in is_risky) but adequate here, because there is no path from a drafted
message to a send that does not pass a human first.
"""

from __future__ import annotations

from typing import Literal, Protocol

from axon.config import Settings, get_settings
from axon.models import MessageDraft

# What kind of message we're asking for. Email has a subject line and room for a
# sentence or two; the chat-shaped channels have neither.
Channel = Literal["email", "chat"]

# The greeting and sign-off are deliberately NOT asked for here — they get assembled in
# code below. Asking a model for them produced "Hi,I'm writing about...Thanks," with the
# newlines silently dropped, and no amount of "on its own line" in a prompt makes
# whitespace reliable. Structure that must be right every time shouldn't be generated.
_EMAIL_PROMPT = """You write the body text of short, plain, professional emails from a
personal note.

Rules:
- Use ONLY what the note says. Never invent a time, place, name, or detail that is not
  in the note. If the note is vague, the email stays vague.
- Subject: a short phrase, no trailing punctuation.
- Body: one or two short sentences of message text ONLY. No greeting, no "Hi", no
  sign-off, no "Thanks", no name, no signature — those are added separately.
- Do not mention that this was generated.
- Drop instruction phrasing ("email bob@x.com about ...") — the reader is the recipient,
  not the person who wrote the note. Never include the recipient's address in the body."""

# Assembled around the model's message text, so the shape of an email is guaranteed
# rather than hoped for.
_EMAIL_GREETING = "Hi,"
_EMAIL_SIGN_OFF = "Thanks,"

_CHAT_PROMPT = """You write short chat/WhatsApp messages from a personal note.

Rules:
- Use ONLY what the note says. Never invent a time, place, name, or detail that is not
  in the note.
- One or two sentences. No greeting, no sign-off, no subject line.
- Leave `subject` empty.
- Drop instruction phrasing ("whatsapp me ...", "tell the team ...") — the reader is the
  recipient, not the person who wrote the note."""


class Drafter(Protocol):
    """Writes the message a sending hand will deliver."""

    name: str

    async def draft(
        self, note_text: str, channel: Channel, fallback: MessageDraft
    ) -> MessageDraft:
        """Return the message to send. `fallback` is the hand's own deterministic
        version, used verbatim when there is no model or the model gives nothing
        usable — so a drafting failure degrades to the old behaviour, never to an
        empty message."""
        ...


class PlainDrafter:
    """No key, no network, no change from pre-Step-18 behaviour."""

    name = "plain"

    async def draft(
        self, note_text: str, channel: Channel, fallback: MessageDraft
    ) -> MessageDraft:
        return fallback


class GeminiDrafter:
    """The optional smarter drafter. Same client setup as GeminiBrain — see
    axon/brain/gemini.py for why it is OpenAIChatCompletionClient and not
    OpenAIChatClient, and docs/adr/0005-gemini-brain.md for the model choice."""

    name = "gemini"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        from agent_framework.openai import OpenAIChatCompletionClient

        from axon.brain.gemini import DEFAULT_MODEL, GEMINI_BASE_URL

        self._client = OpenAIChatCompletionClient(
            model=model or DEFAULT_MODEL, api_key=api_key, base_url=GEMINI_BASE_URL
        )

    async def draft(
        self, note_text: str, channel: Channel, fallback: MessageDraft
    ) -> MessageDraft:
        from agent_framework import ChatOptions, Message

        from axon.models import DraftVerdict

        prompt = _EMAIL_PROMPT if channel == "email" else _CHAT_PROMPT
        try:
            response = await self._client.get_response(
                # Message(role, contents) treats a bare str as an iterable of
                # characters — it must be a list. Same trap as GeminiBrain.classify.
                [Message("system", [prompt]), Message("user", [note_text])],
                options=ChatOptions(response_format=DraftVerdict),
            )
            verdict: DraftVerdict = response.value
        except Exception:  # noqa: BLE001 - a drafting failure must not block the note
            # Deliberately broad: this runs inside prepare(), the half that is supposed
            # to always succeed and never cost anything. A network blip, a quota error
            # or a malformed response should leave the human with the plain draft and a
            # working approval gate, not an exception where a message should be.
            return fallback

        body = (verdict.body or "").strip()
        if not body:
            return fallback

        if channel == "chat":
            return MessageDraft(body=body, subject=None)

        subject = (verdict.subject or "").strip()
        return MessageDraft(
            body=f"{_EMAIL_GREETING}\n\n{body}\n\n{_EMAIL_SIGN_OFF}",
            subject=subject or fallback.subject,
        )


def get_drafter(settings: Settings | None = None) -> Drafter:
    """Gemini if a key is configured, the free plain drafter otherwise.

    Mirrors get_brain(): the import of agent_framework.openai stays inside the
    key-present branch so a machine with no key never pays for it.
    """
    settings = settings or get_settings()
    if settings.gemini_api_key:
        return GeminiDrafter(settings.gemini_api_key)
    return PlainDrafter()
