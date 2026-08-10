"""The brain, as a Microsoft Agent Framework workflow.

classify -> remember -> prepare -> gate -> execute -> persist. `gate` is where risky
notes pause for a human; `prepare` (safe) and `execute` (risky) are the Hand interface's
two halves — see docs/V2-PLAN.md Step 7 and axon/hands/base.py.
See docs/adr/0001-microsoft-agent-framework.md and docs/adr/0003-human-approval-checkpoint.md.

Framework gotchas, learned the hard way (ADR-0001, ADR-0003):

- ctx.send_message, ctx.request_info and ctx.yield_output are all async and must be
  awaited.
- every type that can land in a checkpoint has to be registered by "module:qualname",
  and must live in a real module — never in __main__.
- after a resume, the framework can still report IDLE_WITH_PENDING_REQUESTS even though
  the response handler already produced its output. Do not trust that enum for "is this
  finished" — check get_outputs() instead.
- an ApprovalRequest carries the whole ClassifiedNote, not just an id, because the
  response handler that reads it back may run in a different process, days later, with
  no other state available.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Never

from agent_framework import (
    Executor,
    FileCheckpointStorage,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
    response_handler,
)

from axon.brain.classifier import Brain, first_word, get_brain
from axon.config import Settings, get_settings
from axon.hands.base import Hand
from axon.models import (
    ApprovalOutcome,
    ApprovalRequest,
    Classification,
    ClassifiedNote,
    ClassifyRequest,
    MessageDraft,
    NoteKind,
    PreparedNote,
)

WORKFLOW_NAME = "axon_capture"

# Checkpoint loading is allowlisted by "module:qualname". A type missing from this list
# does not error loudly — the checkpoint just silently fails to load, and
# list_checkpoints() returns an empty list with only a logged warning. This bit twice:
# once for the top-level message types, and again for NoteKind — an enum *field inside*
# Classification — which the pickler stores as its own type, not folded into its parent.
CHECKPOINT_TYPES = [
    f"{model.__module__}:{model.__qualname__}"
    for model in (
        ClassifyRequest, ClassifiedNote, Classification, PreparedNote,
        ApprovalRequest, ApprovalOutcome, NoteKind,
        # Step 18: nested inside PreparedNote/ApprovalRequest/ApprovalOutcome. Nested
        # models need registering in their own right -- the same trap NoteKind hit,
        # where a missing entry produced a silently empty list_checkpoints() rather
        # than an error.
        MessageDraft,
    )
] + [
    # A reminder's due_at is a timezone-aware datetime, and the parser attaches a real
    # IANA zone (dateparser/system-local), not UTC — so this stdlib type rides along
    # too. Found the same way as NoteKind: a silently-empty list_checkpoints().
    "zoneinfo:ZoneInfo",
]

PersistFn = Callable[[ApprovalOutcome], None]
RememberFn = Callable[[ClassifiedNote], None]
# A resolver, not a fixed Hand: `execute` may run after a resume in a brand new
# process (same reason ApprovalRequest carries the whole note, not just an id — see
# ADR-0003), so which hand acted on a note must be re-derivable from the note's text
# alone, never from in-memory state that only existed at prepare time.
HandResolver = Callable[[str], Hand]


def _describe_action(text: str) -> str:
    """A short label for what's being asked, e.g. 'push' from 'push the repo to github'."""
    return first_word(text) or "act on this"


class ClassifyExecutor(Executor):
    """Step one: what even is this note?"""

    def __init__(self, brain: Brain, id: str = "classify") -> None:
        super().__init__(id=id)
        self._brain = brain

    @handler
    async def classify(
        self, request: ClassifyRequest, ctx: WorkflowContext[ClassifiedNote]
    ) -> None:
        result = await self._brain.classify(request.text)
        await ctx.send_message(
            ClassifiedNote(note_id=request.note_id, text=request.text, classification=result)
        )


class RememberExecutor(Executor):
    """Step two: file it away so it can be found later by meaning."""

    def __init__(self, remember: RememberFn, id: str = "remember") -> None:
        super().__init__(id=id)
        self._remember = remember

    @handler
    async def remember(
        self, note: ClassifiedNote, ctx: WorkflowContext[ClassifiedNote]
    ) -> None:
        self._remember(note)
        await ctx.send_message(note)


class PrepareExecutor(Executor):
    """Step three: the safe half of a hand — build, draft, stage. Never risky."""

    def __init__(self, resolve_hand: HandResolver, id: str = "prepare") -> None:
        super().__init__(id=id)
        self._resolve_hand = resolve_hand

    @handler
    async def prepare(
        self, note: ClassifiedNote, ctx: WorkflowContext[PreparedNote]
    ) -> None:
        hand = self._resolve_hand(note.text)
        await ctx.send_message(await hand.prepare(note))


class GateExecutor(Executor):
    """Step four: risky notes stop here and wait for a human. See ADR-0003."""

    def __init__(self, id: str = "gate") -> None:
        super().__init__(id=id)

    @handler
    async def gate(
        self, prepared: PreparedNote, ctx: WorkflowContext[ApprovalOutcome]
    ) -> None:
        if not prepared.note.classification.risky:
            # Nothing to approve — pass straight through. `approved=True` here means
            # "never needed approval", not "a human said yes". prepared.detail carries
            # through too — for a hand like the research one, this IS the deliverable,
            # not just a status note (see ApprovalOutcome's docstring).
            await ctx.send_message(
                ApprovalOutcome(
                    note=prepared.note, approved=True,
                    detail=prepared.detail, project_dir=prepared.project_dir,
                    push_url=prepared.push_url, draft=prepared.draft,
                )
            )
            return
        await ctx.request_info(
            ApprovalRequest(
                note=prepared.note,
                action=_describe_action(prepared.note.text),
                detail=prepared.detail,
                project_dir=prepared.project_dir,
                push_url=prepared.push_url,
                draft=prepared.draft,
            ),
            bool,
        )

    @response_handler
    async def resumed(
        self,
        original_request: ApprovalRequest,
        approved: bool,
        ctx: WorkflowContext[ApprovalOutcome],
    ) -> None:
        await ctx.send_message(
            ApprovalOutcome(
                note=original_request.note, approved=approved,
                detail=original_request.detail, project_dir=original_request.project_dir,
                push_url=original_request.push_url, draft=original_request.draft,
            )
        )


class ExecuteExecutor(Executor):
    """Step five: the risky half of a hand — only runs once approved."""

    def __init__(self, resolve_hand: HandResolver, id: str = "execute") -> None:
        super().__init__(id=id)
        self._resolve_hand = resolve_hand

    @handler
    async def run_execute(
        self, outcome: ApprovalOutcome, ctx: WorkflowContext[ApprovalOutcome]
    ) -> None:
        if outcome.approved:
            hand = self._resolve_hand(outcome.note.text)
            await hand.execute(outcome)
        await ctx.send_message(outcome)


class PersistExecutor(Executor):
    """Step six: record the verdict."""

    def __init__(self, persist: PersistFn, id: str = "persist") -> None:
        super().__init__(id=id)
        self._persist = persist

    @handler
    async def persist(
        self, outcome: ApprovalOutcome, ctx: WorkflowContext[Never, ApprovalOutcome]
    ) -> None:
        self._persist(outcome)
        await ctx.yield_output(outcome)


def checkpoint_storage(settings: Settings) -> FileCheckpointStorage:
    return FileCheckpointStorage(settings.checkpoint_dir, allowed_checkpoint_types=CHECKPOINT_TYPES)


def _default_hand_resolver(settings: Settings) -> HandResolver:
    """Deferred import: axon.hands imports axon.brain.workflow's own models, and
    keeping this out of module scope avoids a circular import at import time.

    Closes over `settings` so a GitHubHand resolved mid-workflow (including after a
    resume in a fresh process) writes into the same projects_dir the caller configured
    — never silently falls back to the real one, which bit tests when this was missed.
    """
    from axon.hands import pick_hand

    return lambda text: pick_hand(text, settings=settings)


def build_workflow(
    persist: PersistFn,
    remember: RememberFn | None = None,
    brain: Brain | None = None,
    hand_resolver: HandResolver | None = None,
    settings: Settings | None = None,
) -> Workflow:
    """classify -> remember -> prepare -> gate -> execute -> persist.

    `remember` is optional so tests can exercise the graph without loading a 134MB
    embedding model. `hand_resolver` maps a note's text to the Hand that should act on
    it — defaulting to `axon.hands.pick_hand`, which returns NoopHand unless the note
    looks GitHub-shaped (Step 8).
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    resolve_hand = hand_resolver or _default_hand_resolver(settings)
    classify = ClassifyExecutor(brain or get_brain(settings))
    recall = RememberExecutor(remember or (lambda _note: None))
    prepare = PrepareExecutor(resolve_hand)
    gate = GateExecutor()
    act = ExecuteExecutor(resolve_hand)
    store = PersistExecutor(persist)

    return (
        WorkflowBuilder(
            name=WORKFLOW_NAME,
            start_executor=classify,
            checkpoint_storage=checkpoint_storage(settings),
        )
        .add_edge(classify, recall)
        .add_edge(recall, prepare)
        .add_edge(prepare, gate)
        .add_edge(gate, act)
        .add_edge(act, store)
        .build()
    )


@dataclass
class PendingApproval:
    """A workflow paused mid-flight, waiting on a human."""

    note_id: int
    request_id: str
    checkpoint_id: str
    # Set when freshly paused. Reconstructing a PendingApproval from the `approvals`
    # table to resume it later has no need for this — resume only needs the ids.
    request: ApprovalRequest | None = None


@dataclass
class CaptureResult:
    """Exactly one of these is set. Never both."""

    completed: ApprovalOutcome | None = None
    pending: PendingApproval | None = None


async def _find_checkpoint_for_request(settings: Settings, request_id: str) -> str:
    """Which checkpoint holds this pending request?

    Not "the latest checkpoint" — that was a spike shortcut. Multiple notes can be
    paused at once, so this matches on request_id specifically.
    """
    checkpoints = await checkpoint_storage(settings).list_checkpoints(workflow_name=WORKFLOW_NAME)
    for ckpt in checkpoints:
        if request_id in (ckpt.pending_request_info_events or {}):
            return ckpt.checkpoint_id
    raise RuntimeError(f"no checkpoint on disk holds request {request_id!r}")


async def _interpret(result, note_id: int, settings: Settings) -> CaptureResult:
    pending_events = result.get_request_info_events()
    if pending_events:
        # V1 has exactly one gate, so exactly one pending request per paused note.
        event = pending_events[0]
        checkpoint_id = await _find_checkpoint_for_request(settings, event.request_id)
        return CaptureResult(
            pending=PendingApproval(
                note_id=note_id,
                request_id=event.request_id,
                checkpoint_id=checkpoint_id,
                request=event.data,
            )
        )

    outputs = result.get_outputs()
    if not outputs:
        raise RuntimeError(f"the brain produced nothing for note {note_id}")
    return CaptureResult(completed=outputs[0])


async def sweep_checkpoints(settings: Settings, keep_ids: set[str]) -> int:
    """Delete every checkpoint for this workflow except the ones still needed.

    Every workflow run writes a checkpoint per superstep, paused or not (see the known
    limit recorded in Step 2). `keep_ids` should be the checkpoint_id of every currently
    *pending* approval — anything else has already been consumed or was never needed.
    Returns how many were deleted.
    """
    storage = checkpoint_storage(settings)
    all_ids = await storage.list_checkpoint_ids(workflow_name=WORKFLOW_NAME)
    deleted = 0
    for checkpoint_id in all_ids:
        if checkpoint_id not in keep_ids:
            await storage.delete(checkpoint_id)
            deleted += 1
    return deleted


async def run_capture(
    note_id: int,
    text: str,
    persist: PersistFn,
    remember: RememberFn | None = None,
    brain: Brain | None = None,
    hand_resolver: HandResolver | None = None,
    settings: Settings | None = None,
) -> CaptureResult:
    """Push one note through the brain. Either it completes, or it's now pending."""
    settings = settings or get_settings()
    workflow = build_workflow(
        persist, remember=remember, brain=brain, hand_resolver=hand_resolver, settings=settings
    )
    result = await workflow.run(ClassifyRequest(note_id=note_id, text=text))
    return await _interpret(result, note_id, settings)


async def resume_capture(
    approval: PendingApproval,
    approved: bool,
    persist: PersistFn,
    remember: RememberFn | None = None,
    brain: Brain | None = None,
    hand_resolver: HandResolver | None = None,
    settings: Settings | None = None,
) -> CaptureResult:
    """Resume a paused note with the human's answer. May run in a fresh process."""
    settings = settings or get_settings()
    workflow = build_workflow(
        persist, remember=remember, brain=brain, hand_resolver=hand_resolver, settings=settings
    )
    result = await workflow.run(
        responses={approval.request_id: approved}, checkpoint_id=approval.checkpoint_id
    )
    return await _interpret(result, approval.note_id, settings)
