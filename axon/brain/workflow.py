"""The brain, as a Microsoft Agent Framework workflow.

Right now it is two steps: work out what the note is, then write that down. It is built
as a real workflow — named, with checkpoint storage wired in — because Step 5 inserts an
approval gate into this same graph. See docs/adr/0001-microsoft-agent-framework.md.

Framework gotchas, learned the hard way (ADR-0001): ctx.send_message and ctx.yield_output
are async and must be awaited, and every type that can land in a checkpoint has to be
registered by "module:qualname".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Never

from agent_framework import (
    Executor,
    FileCheckpointStorage,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)

from axon.brain.classifier import Brain, get_brain
from axon.config import Settings, get_settings
from axon.models import Classification, ClassifiedNote, ClassifyRequest

WORKFLOW_NAME = "axon_capture"

# Checkpoint loading is allowlisted by "module:qualname". A type missing from this list
# does not error loudly — the checkpoint just silently fails to load.
CHECKPOINT_TYPES = [
    f"{model.__module__}:{model.__qualname__}"
    for model in (ClassifyRequest, ClassifiedNote, Classification)
]

PersistFn = Callable[[ClassifiedNote], None]
RememberFn = Callable[[ClassifiedNote], None]


class ClassifyExecutor(Executor):
    """Step one: what even is this note?"""

    def __init__(self, brain: Brain, id: str = "classify") -> None:
        super().__init__(id=id)
        self._brain = brain

    @handler
    async def classify(
        self, request: ClassifyRequest, ctx: WorkflowContext[ClassifiedNote]
    ) -> None:
        result = self._brain.classify(request.text)
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


class PersistExecutor(Executor):
    """Step two: record the verdict."""

    def __init__(self, persist: PersistFn, id: str = "persist") -> None:
        super().__init__(id=id)
        self._persist = persist

    @handler
    async def persist(
        self, note: ClassifiedNote, ctx: WorkflowContext[Never, ClassifiedNote]
    ) -> None:
        self._persist(note)
        await ctx.yield_output(note)


def build_workflow(
    persist: PersistFn,
    remember: RememberFn | None = None,
    brain: Brain | None = None,
    settings: Settings | None = None,
) -> Workflow:
    """classify -> remember -> persist.

    `remember` is optional so tests can exercise the graph without loading a 134MB
    embedding model. The graph shape stays identical either way.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    classify = ClassifyExecutor(brain or get_brain(settings))
    recall = RememberExecutor(remember or (lambda _note: None))
    store = PersistExecutor(persist)

    return (
        WorkflowBuilder(
            name=WORKFLOW_NAME,
            start_executor=classify,
            checkpoint_storage=FileCheckpointStorage(
                settings.checkpoint_dir, allowed_checkpoint_types=CHECKPOINT_TYPES
            ),
        )
        .add_edge(classify, recall)
        .add_edge(recall, store)
        .build()
    )


async def run_capture(
    note_id: int,
    text: str,
    persist: PersistFn,
    remember: RememberFn | None = None,
    brain: Brain | None = None,
    settings: Settings | None = None,
) -> ClassifiedNote:
    """Push one note through the brain and return what it decided."""
    workflow = build_workflow(persist, remember=remember, brain=brain, settings=settings)
    result = await workflow.run(ClassifyRequest(note_id=note_id, text=text))

    outputs = result.get_outputs()
    if not outputs:
        raise RuntimeError(f"the brain produced nothing for note {note_id}")
    return outputs[0]
