"""The chat hand: draft locally (safe), post to Slack/Teams only on approval (risky).

See docs/V2-PLAN.md Step 14. One hand covers both platforms, not two — Slack's and
(classic) Teams' incoming webhooks both accept the same simple {"text": ...} JSON body,
so which platform you're actually talking to is just whichever URL you configured.

`prepare` never touches the network — it just drafts the message text from the note,
entirely offline. `execute` is the only place a real post happens, and only runs once
approved.
"""

from __future__ import annotations

import re

import httpx

from axon.config import Settings, get_settings
from axon.models import ApprovalOutcome, ClassifiedNote, PreparedNote

# A note routes to ChatHand if it names a platform, or uses one of the "tell/message/
# notify the team" phrasings that clearly mean "post this somewhere for people to see."
_PLATFORM_WORDS = {"slack", "teams"}
_TEAM_PHRASES = ("tell the team", "message the team", "notify the team", "post to the team")

_LEAD_IN = re.compile(
    r"^(please\s+)?(tell|message|notify)\s+the\s+team\s+(that\s+)?"
    r"|^(please\s+)?post\s+(to\s+the\s+team|in\s+(slack|teams)|on\s+(slack|teams))\s+(that\s+)?",
    re.IGNORECASE,
)


def looks_like_a_chat_note(text: str) -> bool:
    lowered = text.lower()
    words = set(re.findall(r"[a-z]+", lowered))
    return bool(words & _PLATFORM_WORDS) or any(phrase in lowered for phrase in _TEAM_PHRASES)


def _draft(text: str) -> str:
    """Pull a clean message out of the note text. Deterministic, no AI — same "free by
    default" pattern as the mock brain, the mock GitHub builder, and the email hand."""
    return _LEAD_IN.sub("", text).strip() or text


class ChatHand:
    """`prepare`: draft locally. `execute`: real webhook post, only once approved."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        message = _draft(note.text)
        return PreparedNote(note=note, detail=f"chat message ready: {message!r}")

    async def execute(self, outcome: ApprovalOutcome) -> None:
        """The risky half. Only ever called with an approved outcome — see
        ExecuteExecutor.run_execute in axon/brain/workflow.py.

        Re-derives the message from the note rather than trusting anything carried
        in-memory from prepare(), same reasoning as GitHubHand and EmailHand: this can
        run in a fresh process after a resume.
        """
        if not self._settings.chat_webhook_url:
            raise RuntimeError(
                "CHAT_WEBHOOK_URL is not set in .env — Axon doesn't know where to post. "
                "The draft is safe; set it and re-approve."
            )

        message = _draft(outcome.note.text)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._settings.chat_webhook_url, json={"text": message}, timeout=10.0
            )
            response.raise_for_status()
