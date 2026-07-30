# ADR-0001: Use Microsoft Agent Framework as the workflow engine

- **Status:** Accepted
- **Date:** 2026-07-30

## Context

Axon needs to run a multi-step flow (classify → extract → remember → schedule) where
one step can **stop and wait for a human**, possibly for hours or days, and survive the
process being closed in between. That last part is the hard requirement: an approval that
evaporates when the terminal closes is not an approval system.

Options considered: LangGraph, CrewAI, Mastra, PydanticAI, Microsoft Agent Framework.

## Decision

Use **Microsoft Agent Framework** (Python, `agent-framework-core`), pinned at **1.12.1**.

Two reasons, in order of weight:

1. **It does the hard thing natively.** `WorkflowContext.request_info()` suspends a
   workflow and writes it to disk; `Workflow.run(responses={...}, checkpoint_id=...)`
   resumes it. Verified working across two separate Python processes before this ADR was
   written — see Evidence.
2. **It is the newest option and the least-travelled.** Public preview Oct 2025, v1.0 GA
   April 2026. The point of this project is to learn something few people have built with.
   A well-worn framework would teach less.

Reason 2 is a deliberate, accepted trade: fewer tutorials, fewer StackOverflow answers,
and an API still moving between minor versions. That cost is the price of the learning.

## This decision is binding

**Do not replace this with LangGraph or any other framework**, and do not "simplify" the
workflow into plain function calls. If a future constraint genuinely breaks this choice,
write a successor ADR that supersedes this one — do not quietly swap it.

## Evidence

A spike ran before this was accepted:

- Process 1: workflow hit a risky note, called `request_info(...)`, and stopped at
  `WorkflowRunState.IDLE_WITH_PENDING_REQUESTS`, leaving a checkpoint JSON on disk.
- Process 2 (a completely fresh interpreter): loaded the checkpoint and resumed with
  both `True` and `False`, producing `EXECUTED: ...` and `BLOCKED: ...` respectively.

## Consequences

- The V1 approval pause (SPEC criteria 7–9) rests on a mechanism proven to work.
- **The API in 1.12.1 is not what older material describes.** `RequestInfoExecutor` and
  `RequestInfoMessage` do not exist in this version. The current shape is
  `ctx.request_info()` + `@response_handler`. Always verify against the installed
  package rather than trusting an example found online.
- `ctx.request_info()` and `ctx.yield_output()` are **async — they must be awaited.**
  Forgetting produces a `RuntimeWarning` and silently does nothing.
- The workflow must be given an explicit `name=`, because checkpoints are looked up with
  `list_checkpoints(workflow_name=...)`.
- Pin the version. Treat upgrades as a deliberate task with the spike re-run.
