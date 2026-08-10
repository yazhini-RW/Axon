"""The email hand: draft locally (safe), send via SMTP only on approval (risky).

See docs/V2-PLAN.md Step 13. `prepare` never touches the network — it just extracts a
recipient and drafts a subject/body from the note text, entirely offline. `execute` is
the only place a real email leaves the machine, and only runs once approved.

Free via a Gmail "app password" (myaccount.google.com/apppasswords — not the real
account password) over SMTP+STARTTLS. No paid email API, no key beyond that password.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
from email.message import EmailMessage

from axon.brain.drafter import Drafter, get_drafter
from axon.config import Settings, get_settings
from axon.models import ApprovalOutcome, ClassifiedNote, MessageDraft, PreparedNote

# Appended to every outgoing body, whoever drafted it. Honest disclosure that a program
# sent this, and kept out of the drafter so a model cannot quietly drop it.
SIGNATURE = "\n\n-- \nSent by Axon on your behalf, after your approval."

# A note routes to EmailHand only if it both mentions emailing AND contains an actual
# address to send to — "the office wifi password is on the whiteboard" or "my email is
# private" must never route here just because a stray word matches.
_EMAIL_WORDS = {"email", "mail"}
_ADDRESS_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def looks_like_an_email_note(text: str) -> bool:
    lowered = text.lower()
    words = set(re.findall(r"[a-z]+", lowered))
    return bool(words & _EMAIL_WORDS) and bool(_ADDRESS_PATTERN.search(text))


# Strips the phrasing that introduces the address ("email ", "send an email to "),
# not just the address itself — otherwise the subject line comes out as "email about
# the quarterly report" instead of "about the quarterly report". No nested/unbounded
# quantifiers here on purpose: this runs on arbitrary user text, and a pattern like
# (\s+\w+)*?\s+to\s+ risks catastrophic backtracking on adversarial input — the same
# category of risk the prompt-injection finding in ADR-0005 is a reminder to take
# seriously for anything that parses note text. Each alternative below is fixed-width.
_LEAD_IN = re.compile(
    r"^(please\s+)?send\s+(an\s+|the\s+)?(email|mail)\s+to\s+"
    r"|^(email|mail)\s+to\s+"
    r"|^(email|mail)\s+",
    re.IGNORECASE,
)
# Handles phrasing where the address comes last ("mail the report to <addr>") — after
# the address is removed, "to" is left dangling at the end.
_TRAILING_TO = re.compile(r"\s+to\s*$", re.IGNORECASE)


def _draft(text: str) -> tuple[str, str, str]:
    """Pull (recipient, subject, body) out of the note text. Deterministic, no AI —
    same "free by default" pattern as the mock brain and the mock GitHub builder."""
    match = _ADDRESS_PATTERN.search(text)
    recipient = match.group(0) if match else ""

    # Everything except the address and the phrasing that introduced it, trimmed,
    # becomes the subject (short) and the body (the note as written — Axon never
    # invents what you meant).
    remainder = _ADDRESS_PATTERN.sub("", text).strip()
    remainder = _LEAD_IN.sub("", remainder).strip()
    remainder = _TRAILING_TO.sub("", remainder).strip()

    words = remainder.split()
    subject = " ".join(words[:8]) or "a note from Axon"
    if len(words) > 8:
        subject += "..."

    # Without the signature: prepare() appends it after drafting, so the body carried
    # through the checkpoint is byte-for-byte the body that reaches the inbox.
    body = text
    return recipient, subject, body


class EmailHand:
    """`prepare`: draft locally. `execute`: real SMTP send, only once approved."""

    def __init__(
        self, settings: Settings | None = None, drafter: Drafter | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._drafter = drafter or get_drafter(self._settings)

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        recipient, subject, body = _draft(note.text)
        draft = await self._drafter.draft(
            note.text, "email", MessageDraft(subject=subject, body=body)
        )
        # The signature goes on here rather than in the drafter, so the body carried
        # through the checkpoint is exactly the body that reaches the inbox.
        draft = draft.model_copy(update={"body": draft.body + SIGNATURE})
        return PreparedNote(
            note=note,
            detail=f"draft ready — to: {recipient}, subject: {draft.subject!r}",
            draft=draft,
        )

    async def execute(self, outcome: ApprovalOutcome) -> None:
        """The risky half. Only ever called with an approved outcome — see
        ExecuteExecutor.run_execute in axon/brain/workflow.py.

        Re-derives the recipient/subject/body from the note rather than trusting
        anything carried in-memory from prepare(), because this can run in a fresh
        process after a resume — same reasoning as GitHubHand.execute() (Step 9).
        """
        note = outcome.note
        recipient, subject, body = _draft(note.text)
        # Prefer the approved draft over re-deriving. Re-derivation is safe only when
        # deterministic; a Gemini-written draft is not, so sending a fresh one would
        # send something the human never saw. See MessageDraft's docstring.
        if outcome.draft is not None:
            subject = outcome.draft.subject or subject
            body = outcome.draft.body
        else:
            body += SIGNATURE

        if not self._settings.email_address or not self._settings.email_app_password:
            raise RuntimeError(
                "EMAIL_ADDRESS / EMAIL_APP_PASSWORD are not set in .env — Axon doesn't "
                f"know how to send as you. The draft to {recipient} is safe; set both "
                "and re-approve."
            )

        message = EmailMessage()
        message["From"] = self._settings.email_address
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)

        # smtplib is synchronous. prepare/execute run inside the workflow's own event
        # loop (see workflow.py's module docstring) — a blocking network call there
        # would stall every other note, same trap as GitHubHand's git subprocess calls.
        await asyncio.to_thread(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(self._settings.email_address, self._settings.email_app_password)
            smtp.send_message(message)
