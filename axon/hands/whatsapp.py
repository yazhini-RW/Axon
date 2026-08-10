"""The WhatsApp hand: draft locally (safe), send via Meta's Cloud API on approval (risky).

See docs/V2-PLAN.md Step 17. Same two-half shape as the email and chat hands: `prepare`
never touches the network, `execute` is the only place a real message leaves the machine.

Free on Meta's test-number tier — a test sender Meta provides, able to message only the
handful of numbers verified in their dashboard. Going beyond that costs money, so nothing
here tries to.

The constraint that shapes this module: **a business cannot send arbitrary text to someone
out of the blue.** WhatsApp only allows free-form text inside a 24-hour window opened by
the *user* messaging the business first; outside it, everything must go through a template
approved in advance. Axon initiating a message is always outside that window, so this hand
always sends a template — with the note's text passed in as a `{{1}}` body parameter, if
the configured template takes one. That's not a limitation of the test tier; it's
WhatsApp's anti-spam rule and applies at every tier.

Deliberately a separate hand from ChatHand rather than an extension of it: ChatHand posts
a flat {"text": ...} to whatever webhook URL you configured and doesn't care who's on the
other end, whereas this needs a token, a sender id, a recipient and a template name, and
speaks a completely different request shape.
"""

from __future__ import annotations

import re

import httpx

from axon.brain.drafter import Drafter, get_drafter
from axon.config import Settings, get_settings
from axon.models import ApprovalOutcome, ClassifiedNote, MessageDraft, PreparedNote

GRAPH_VERSION = "v21.0"

# A note routes here only if it actually names WhatsApp. Deliberately narrower than the
# other hands' matching: "message" or "text" alone are far too broad ("text me the wifi
# password" could as easily mean SMS), and a wrong guess here sends a real message to a
# real phone rather than merely filing a note oddly.
_WHATSAPP_WORDS = {"whatsapp", "wa"}

# Strips the phrasing that introduces the request, so the delivered message reads as the
# note itself rather than an instruction to Axon: "whatsapp me the wifi password" should
# arrive as "the wifi password", not with the "whatsapp me" still attached. Every
# alternative is fixed-width — no nested quantifiers on arbitrary user text, same
# reasoning as the email hand's _LEAD_IN.
_LEAD_IN = re.compile(
    r"^(please\s+)?(whatsapp|wa)\s+me\s+(that\s+|about\s+)?"
    r"|^(please\s+)?send\s+(me\s+)?(a\s+)?(whatsapp|wa)\s+(message\s+)?(about\s+|saying\s+)?"
    r"|^(please\s+)?(whatsapp|wa)\s+",
    re.IGNORECASE,
)


def looks_like_a_whatsapp_note(text: str) -> bool:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return bool(words & _WHATSAPP_WORDS)


def _draft(text: str) -> str:
    """Pull the message out of the note text. Deterministic, no AI — same "free by
    default" pattern as the mock brain, the mock GitHub builder, email and chat."""
    return _LEAD_IN.sub("", text).strip() or text


class WhatsAppHand:
    """`prepare`: draft locally. `execute`: real Cloud API send, only once approved."""

    def __init__(
        self, settings: Settings | None = None, drafter: Drafter | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._drafter = drafter or get_drafter(self._settings)

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        draft = await self._drafter.draft(
            note.text, "chat", MessageDraft(body=_draft(note.text))
        )
        message = draft.body
        to = self._settings.whatsapp_to
        # Only the last 4 digits: this string goes into the approvals table and onto the
        # web UI, and the full number doesn't need to be either place to confirm intent.
        where = f"...{to[-4:]}" if to else "no recipient configured"
        return PreparedNote(
            note=note,
            detail=f"whatsapp ready — to: {where}, message: {message!r}",
            draft=draft,
        )

    async def execute(self, outcome: ApprovalOutcome) -> None:
        """The risky half. Only ever called with an approved outcome — see
        ExecuteExecutor.run_execute in axon/brain/workflow.py.

        Re-derives the message from the note rather than trusting anything carried
        in-memory from prepare(), same reasoning as every other hand: this can run in a
        fresh process after a resume.
        """
        settings = self._settings
        missing = [
            name
            for name, value in (
                ("WHATSAPP_TOKEN", settings.whatsapp_token),
                ("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id),
                ("WHATSAPP_TO", settings.whatsapp_to),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"{' / '.join(missing)} not set in .env — Axon can't send the WhatsApp. "
                "The draft is safe; set them and re-approve."
            )

        # The approved draft, not a re-derivation -- see MessageDraft's docstring.
        message = outcome.draft.body if outcome.draft else _draft(outcome.note.text)
        url = (
            f"https://graph.facebook.com/{GRAPH_VERSION}/"
            f"{settings.whatsapp_phone_number_id}/messages"
        )
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": settings.whatsapp_to,
                    "type": "template",
                    "template": {
                        "name": settings.whatsapp_template,
                        "language": {"code": "en_US"},
                        "components": [
                            {
                                "type": "body",
                                "parameters": [{"type": "text", "text": message}],
                            }
                        ],
                    },
                },
                timeout=15.0,
            )

        if response.is_error:
            # Meta's errors are specific and worth surfacing ("template does not exist",
            # "recipient not in allowed list", "token expired") — a bare status code sends
            # you to the dashboard guessing. The token is in the request headers, never in
            # the response body, so echoing the body here leaks nothing.
            raise RuntimeError(
                f"WhatsApp send failed ({response.status_code}): {response.text[:400]}"
            )
