"""Axon's core data shapes.

These live in a real importable module on purpose. Anything that can end up inside a
workflow checkpoint must NOT be defined in __main__ — checkpoint loading is allowlisted
by "module:qualname". See docs/adr/0003-human-approval-checkpoint.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel


class NoteKind(str, Enum):
    """What a note turns out to be. Everything starts UNCLASSIFIED."""

    UNCLASSIFIED = "unclassified"
    FACT = "fact"           # something to remember: "we chose Agent Framework"
    TASK = "task"           # something to do, no specific time
    REMINDER = "reminder"   # something to do at a specific time


class NoteStatus(str, Enum):
    CAPTURED = "captured"            # written down, not yet understood
    CLASSIFIED = "classified"        # the brain has worked out what it is
    SCHEDULED = "scheduled"          # a reminder with a job waiting to fire
    AWAITING_APPROVAL = "awaiting_approval"  # paused, needs a human OK
    DONE = "done"
    MISSED = "missed"                # was due so long ago that firing it would be noise
    BLOCKED = "blocked"              # the human said no


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Note:
    """One thing you told Axon."""

    text: str
    id: int | None = None
    kind: NoteKind = NoteKind.UNCLASSIFIED
    status: NoteStatus = NoteStatus.CAPTURED
    due_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def due_at_local(self) -> datetime | None:
        """due_at is stored in UTC; show it in the machine's own timezone."""
        return self.due_at.astimezone() if self.due_at else None


# --- messages that travel through the workflow -------------------------------------
# These are pydantic rather than dataclasses because the framework serialises them into
# checkpoints. Keeping them here (never in __main__) is what makes a paused workflow
# resumable at all — see docs/adr/0003-human-approval-checkpoint.md.


class Classification(BaseModel):
    """What the brain decided about a note."""

    kind: NoteKind
    due_at: datetime | None = None
    recurring: bool = False
    risky: bool = False
    reason: str = ""


class GeminiVerdict(BaseModel):
    """The shape Gemini is asked to return, before Axon converts it to a Classification.

    Deliberately not the same model as Classification: due_at here is a plain string
    because it comes straight from an LLM's structured-output JSON, and free-text time
    parsing is exactly what axon.brain.classifier already does well with dateparser.
    Reusing that logic (rather than trusting Gemini's own date arithmetic) means both
    brains land on the same, already-tested notion of "when".
    """

    kind: NoteKind
    when: str | None = None  # e.g. "tomorrow morning" — parsed by extract_due(), not here
    recurring: bool = False
    risky: bool = False
    reason: str = ""


class MessageDraft(BaseModel):
    """The actual message a sending hand will deliver, written before the gate.

    Step 18. Exists so the human approves *the message itself*, not a one-line summary
    of it — and, critically, so `execute()` sends exactly what was on screen. Every
    other hand re-derives its work from the note text at execute time (see
    GitHubHand.execute's docstring), which is safe only because that derivation is
    deterministic. Drafting with an LLM is not: re-deriving would send a different
    message than the one approved. So the draft rides through the checkpoint instead.

    `subject` is None for channels that have no such thing (WhatsApp, chat).
    """

    body: str
    subject: str | None = None


class DraftVerdict(BaseModel):
    """The shape the LLM is asked to return, before it becomes a MessageDraft.

    Separate from MessageDraft for the same reason GeminiVerdict is separate from
    Classification: structured output is easier to get right with plain required
    fields, so `subject` is an empty string here rather than a nullable one.
    """

    subject: str = ""
    body: str = ""


class ClassifyRequest(BaseModel):
    """Goes into the workflow."""

    note_id: int
    text: str


class ClassifiedNote(BaseModel):
    """Comes out of it."""

    note_id: int
    text: str
    classification: Classification


class PreparedNote(BaseModel):
    """What a Hand did in its safe `prepare` half, before anything risky happens.

    Carried through the gate into `execute`, so it must stay in this module and be
    registered in axon.brain.workflow.CHECKPOINT_TYPES, same as ApprovalRequest. See
    ADR-0003 and docs/V2-PLAN.md Step 7.
    """

    note: ClassifiedNote
    detail: str = ""  # e.g. "committed to ./projects/12-foo" — shown in `axon approvals`
    # Set only by GitHubHand.prepare() (Step 8). None for every other hand, including
    # NoopHand — `execute` checks this before doing anything push-shaped.
    project_dir: str | None = None
    push_url: str | None = None
    # Step 18. Set by the sending hands (email/whatsapp/chat); None for the rest.
    draft: MessageDraft | None = None


class ApprovalRequest(BaseModel):
    """What Axon is asking permission for.

    Carries the full ClassifiedNote, not just an id, because the response handler that
    reads this back is a separate invocation (possibly a different process, days later)
    and cannot rely on any in-memory state to still be around. Carried inside a workflow
    checkpoint, so it must stay in this module and be registered in
    axon.brain.workflow.CHECKPOINT_TYPES. See ADR-0003.

    `detail` / `project_dir` / `push_url` mirror PreparedNote's fields (Step 8) — carried
    here too so `axon approvals` can show exactly what was built and exactly what a
    `git push` would run, before the human approves. See docs/V2-PLAN.md Step 9's
    "no surprises" requirement.
    """

    note: ClassifiedNote
    action: str
    detail: str = ""
    project_dir: str | None = None
    push_url: str | None = None
    draft: MessageDraft | None = None


class ApprovalOutcome(BaseModel):
    """What happened once the human answered — or, for a non-risky note, what the
    hand's prepare() produced, since the gate passes those straight through.

    `detail` / `project_dir` / `push_url` mirror PreparedNote and ApprovalRequest
    (Steps 8-9). Added in Step 16: without these, a hand whose entire deliverable IS
    its prepare-time output — the research hand's answer text, with no side effect to
    point to instead — had that output silently discarded for every non-risky note,
    which for research is *every* note (a lookup query essentially never contains a
    risky verb). Found by actually running `axon add "what is python"` and seeing the
    real DuckDuckGo answer vanish instead of being shown.
    """

    note: ClassifiedNote
    approved: bool
    detail: str = ""
    project_dir: str | None = None
    push_url: str | None = None
    draft: MessageDraft | None = None
