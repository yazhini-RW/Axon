# ADR-0003: Risky actions always pause for a human

- **Status:** Accepted
- **Date:** 2026-07-30

## Context

Axon's whole point is that it *acts* — and the roadmap has it paying, sending email,
pushing code, and deploying. An agent that does those on its own judgement is a liability.
This constraint exists from V1, before there is anything genuinely dangerous to do,
because retrofitting a safety checkpoint after the fact never works.

## Decision

**No risky action executes without an explicit human OK.**

An action is risky if it spends money, contacts another person, changes a remote system,
or is hard to undo. Concretely, V1 flags: `pay`, `buy`, `order`, `purchase`, `send`,
`email`, `push`, `deploy`, `publish`, `delete`, `post`.

### How the pause works

The workflow calls `await ctx.request_info(ApprovalRequest(...), bool)`. The framework
suspends the run and writes a checkpoint to disk. Nothing downstream executes. The user
later runs `axon approve <id>` or `axon reject <id>`, which resumes the workflow — in a
**different process, possibly days later** — via
`run(responses={request_id: True/False}, checkpoint_id=...)`.

The paused state lives on disk, not in memory. Closing the terminal does not lose it,
and does not silently let the action through either.

### Fail closed

If anything goes wrong — checkpoint unreadable, approval ambiguous, classification
uncertain — the action **does not happen**. The safe default is always "don't".

### Approval state is tracked in SQLite, not inferred from the framework

Axon records `pending / approved / rejected / done` in its own table.

Reason, found during verification: after resuming, the framework still reported
`IDLE_WITH_PENDING_REQUESTS` even though the response handler had run and produced its
output. That state enum is therefore **not** a reliable "is this finished" signal. Axon
uses its own row plus the workflow's `get_outputs()` instead. This also gives
`axon approvals` a straightforward thing to list.

## Constraints on the implementation

- Types carried in an approval request **must live in a real importable module** (e.g.
  `axon.models`), never in `__main__`. Checkpoint deserialization is allowlisted by
  `"module:qualname"`, and Axon must register those types via
  `FileCheckpointStorage(..., allowed_checkpoint_types=[...])`. A type defined in
  `__main__` cannot be resumed — verified: the checkpoint silently fails to load and
  `list_checkpoints()` returns an empty list with only a `logger.warning`.
- **The allowlist reaches into nested fields, not just top-level message types.**
  `NoteKind`, an enum used as a *field* inside `Classification`, needed its own entry —
  the pickler stores it as its own type rather than folding it into its parent. A note
  classified as a fact or task (no enum-adjacent surprises) paused and resumed fine in
  testing; only once a risky note carrying `NoteKind.TASK` was captured did the missing
  entry surface, as a silently-empty `list_checkpoints()` again.
- **`from __future__ import annotations` breaks `@response_handler`.** With it enabled,
  `ctx`'s annotation is a string at decoration time, `_is_workflow_context_type` returns
  false, and registration raises `ValueError: ... got WorkflowContext[ApprovalOutcome]` —
  the message even echoes back the correct-looking annotation, which makes it look like
  a false alarm. Plain `@handler` methods tolerate this; only `@response_handler` does
  not. `axon/brain/workflow.py` does not use the future import for this reason.
- Approvals must be greppable. Every pause and every resume is recorded.

## Consequences

- V1 has no truly dangerous hand, so the checkpoint is exercised on a harmless stand-in.
  That is intentional: the mechanism is proven before it guards anything real.
- V2's GitHub push plugs into an approval gate that already works.
- Slight friction by design. Axon asking twice is much better than Axon buying twice.
