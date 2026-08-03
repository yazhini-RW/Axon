# Axon — V2 Plan and Session Handoff

**Read this first if you are a new session picking up this project.**

Last updated: 2026-07-31, at the end of V1 (commit `b7235d6`).

---

## 1. How to work with me (the human)

- I'm a software engineer at RandomWalk AI. I build side projects to learn new tech and
  prove real engineering skill.
- **Explain in SIMPLE plain English.** When you use a technical term, define it in one
  short line. I don't know a lot of jargon.
- **I'm cost-sensitive.** Use only free tools and free tiers. No paid API keys. Always
  add a mock/offline fallback so everything runs with zero paid services.
- **Keep everything LOCAL.** Do not push to any remote or deploy unless I explicitly say so.
- **Build ONE feature at a time.** Get it working before adding the next.
- **Plan before executing.** Show me the plan and wait for my go-ahead.
- My goals for every project: something UNIQUE, real engineering (not a "prompt wrapper"
  anyone could dismiss as "she just prompted this with AI"), and learning NEW tech.

## 2. What Axon is

A personal "second brain" that remembers notes **and acts on them**, and that always
**pauses and asks before doing anything risky** (pay / send / push / deploy).

I chose **Microsoft Agent Framework** on purpose because it is the newest option
(public preview Oct 2025, v1.0 GA April 2026) — I wanted to learn something few people
have built with. **This is a deliberate choice. Do not talk me back into LangGraph.**

## 3. Current state: V1 is COMPLETE

7 commits, 91 tests passing, all local, nothing ever pushed anywhere.

```
b7235d6 Step 6: optional Gemini brain, same interface, mock stays default
c6b2c98 Step 5: the human approval checkpoint, proven across process boundaries
0aced9f Step 4: the scheduler fires reminders as real notifications
a2376ce Step 3: long-term memory and meaning-based `axon recall`
ef0e3db Step 2: the brain classifies notes inside an Agent Framework workflow
188563a Step 1: capture notes to SQLite, with `axon add` and `axon list`
1be4b8b Step 0: project skeleton, pinned stack, spec and binding ADRs
```

### What works today

| Command | Does |
|---|---|
| `axon add "<note>"` | capture → classify → remember → schedule (or pause if risky) |
| `axon list` | show notes with kind, due time, status |
| `axon recall "<query>"` | meaning-based search, ranked with honest confidence |
| `axon run` | daemon; fires reminders as real Windows notifications |
| `axon approvals` | show what's waiting on your OK |
| `axon approve <id>` / `axon reject <id>` | the human checkpoint |
| `axon doctor` | show config, mode, whether anything costs money |

### The workflow today

`classify → remember → gate → persist` — a real Microsoft Agent Framework workflow in
[axon/brain/workflow.py](../axon/brain/workflow.py).

### Setup

```bash
.venv\Scripts\activate          # venv already exists, Python 3.11.9
pytest -q                       # 91 tests
pytest -q -m "not slow"         # skip the 10 that load a 134MB model
```

### Why each finished step is the way it is

The commit messages are long and deliberately explain the reasoning, the bugs found, and
the decisions changed mid-step. **Read them before altering existing behaviour:**

```bash
git log                         # full messages, not --oneline
git show <hash>                 # one step in detail
```

Short version of what each step settled:

- **Step 0** — verified the framework's real API by spike rather than assumption, and
  found the human-in-the-loop API is nothing like older material describes.
- **Step 1** — SQLite with WAL (so daemon + CLI coexist) and `PRAGMA user_version`
  migrations. Currently at schema **v2**.
- **Step 2** — the rule-based brain. Most of its complexity exists because `dateparser`
  cannot be trusted with raw text.
- **Step 3** — memory. Ranking is **relative, not absolute**, because measured scores
  bunch into a narrow 0.4–0.7 band and fixed cutoffs failed in both directions.
- **Step 4** — the scheduler design changed mid-step; see ADR-0004 for why the
  persistent job store was the wrong tool.
- **Step 5** — the approval gate, proven to resume across separate processes. Also fixed
  the checkpoint leak from Step 2.
- **Step 6** — Gemini behind the same `Brain` interface. Made `Brain.classify()` async
  for both brains. Risk detection is never trusted to the LLM alone.

---

## 4. Read these before changing anything

The ADRs in [docs/adr/](adr/) are **binding**. They exist so the architecture doesn't
drift, and several record traps found the hard way.

| ADR | Key point |
|---|---|
| [0001](adr/0001-microsoft-agent-framework.md) | Microsoft Agent Framework is locked in. Don't swap it. Version pinned at 1.12.1. |
| [0002](adr/0002-free-local-only-stack.md) | Free/local only. **Local Qdrant allows ONE process at a time** — memory must be opened per-command and closed. The `axon run` daemon must never touch it. |
| [0003](adr/0003-human-approval-checkpoint.md) | Risky actions always pause. Checkpoint types must be allowlisted by `module:qualname` and never live in `__main__`. |
| [0004](adr/0004-scheduler-design.md) | SQLite is the source of truth for reminders, not APScheduler's job store. |
| [0005](adr/0005-gemini-brain.md) | Gemini via OpenAI-compatible endpoint. **Now tested against a real key (2026-08-03)** — two bugs and a safety gap found, see "What real testing found" in the ADR. |

Also read [docs/SPEC.md](SPEC.md) — acceptance criteria and measured known limits.

### Framework traps already discovered (do not rediscover these)

- `ctx.request_info()`, `ctx.send_message()` and `ctx.yield_output()` are **async** —
  forgetting `await` silently does nothing.
- `@response_handler` **breaks** under `from __future__ import annotations`. That is why
  `axon/brain/workflow.py` does not use the future import.
- Checkpoint allowlist reaches into **nested fields** — `NoteKind` (enum inside
  `Classification`) and `zoneinfo.ZoneInfo` (on a reminder's `due_at`) each needed their
  own entry. Missing entries fail **silently**: `list_checkpoints()` just returns empty.
- `Message(role, contents)` treats a bare string as an iterable of **characters**. Must
  be `Message("user", [text])` or the prompt is sent one letter at a time, silently.
- APScheduler ignores `replace_existing` while the scheduler is **stopped**, queueing
  duplicates in a pending list.
- `dateparser` parses the bare words "we", "to" and "on" as valid dates. Never hand it
  raw text — a strict regex extracts genuine time expressions first.
- `Executor` (the framework base class) already has a method named `execute`. Naming a
  `@handler` method `execute` silently breaks message dispatch — the framework calls its
  own `execute` with different arguments, and you get
  `TypeError: got an unexpected keyword argument 'trace_contexts'` with a confusing
  traceback pointing into `_edge_runner.py`, not your code. Found in Step 7's
  `ExecuteExecutor`; the handler method is named `run_execute` instead.
- FastAPI runs sync routes in a threadpool, so requests can genuinely run concurrently
  in ways the single-process CLI never did. Two traps that only showed up by actually
  running `axon serve` and driving the UI in a real browser, never in the test suite:
  SQLite's default is to fail immediately with "database is locked" instead of waiting
  (fixed with `PRAGMA busy_timeout` in `repo.connect()`), and two connections racing
  `init_db()` on a brand-new database can both try the same `ALTER TABLE ADD COLUMN`
  migration, so the second one fails with "duplicate column name" (fixed by treating
  that specific error as "someone else already migrated it").
- `agent_framework.openai.OpenAIChatClient` targets OpenAI's newer Responses API
  (`.../responses`), which Gemini's OpenAI-compatible endpoint 404s on — it only
  implements the classic Chat Completions API. Use `OpenAIChatCompletionClient` instead
  (same constructor, same `get_response()`). Found by curling both paths directly
  against a real key, not by reading either SDK's docs. See ADR-0005.
- A live prompt injection got Gemini to agree its own note wasn't risky, and the
  `_RISKY_VERBS` keyword floor didn't catch it either ("wire" was missing). The floor
  is not a promise of completeness — expect to keep raising it. See ADR-0005.

---

## 5. THE V2 PLAN (what to build next)

### The core idea: every hand splits into "safe" and "risky"

This is the design that makes all future hands work, not just this one:

| Hand | **Prepare** (safe, runs immediately) | **Execute** (risky, needs approval) |
|---|---|---|
| **GitHub** (V2) | build the code, commit locally | `git push` |
| Email (V3) | write the draft | send it |
| Shopping (V3) | add to cart | pay |

The existing approval gate sits between the two columns. The brain never changes; each
new hand just fills in a row.

**New workflow shape:** `classify → remember → prepare → gate → execute → persist`

### The safety design

When Axon spawns Claude Code, it passes `--allowedTools` restricted to file editing plus
`git init`, `git add`, `git commit` — **deliberately not `git push`, and not arbitrary
shell commands**. The sub-agent is *structurally incapable* of pushing even if it
misbehaves. Axon holds the only path to the push.

**Verify this restriction actually blocks a push before trusting it.** Do not assume.

### Cost: only ONE part costs money

Everything is free except spawning Claude Code to write code. So, exactly like V1's
mock-brain pattern:

- **Mock builder (default, free):** writes a small real project, runs `git init`,
  `git add`, `git commit`. Real files, real commit — just not AI-authored.
- **Claude Code builder (costs money):** same interface, only when explicitly enabled.

This means the **entire V2 flow can be built and tested for free**, including a real
push to a real GitHub repo. The paid part is turned on once, at the very end.

### Step order (agreed)

| Step | What | Cost |
|---|---|---|
| **7** ✅ | Hand interface (`prepare` / `execute` split). Trivial no-op hand so nothing behaves differently yet and all 91 tests stay green. | free |
| **8** ✅ | GitHub hand — *prepare* half. Build + commit locally with the **mock builder**, then pause. No push. | free |
| **9** ✅ | GitHub hand — *execute* half. Real `git push` on approval. Reject leaves the commit sitting locally. | free |
| **10** ✅ | FastAPI backend exposing the same operations over HTTP. | free |
| **11** ✅ | The web UI — plain HTML served by FastAPI. No npm, no build step, works offline. | free |
| **12** | **Only then:** enable the real Claude Code builder and test once. | costs a little |

**Why hands before UI:** Step 7 changes the workflow shape. Building the UI first would
mean rebuilding parts of it afterwards. This way the UI is built once, against the final
shape.

### The UI (Steps 10-11)

One page with: a box to type a note, the notes list, a search box for recall, and pending
approvals with **Approve / Reject** buttons.

The hardest UI problem is already solved by V1's design: Qdrant is single-process and
SQLite is in WAL mode, so the web server, the CLI, and the `axon run` daemon can all
coexist — as long as the web server also opens and closes memory per request, exactly
like the CLI does.

### Practical constraints

- **`gh` (GitHub CLI) is NOT installed.** Axon cannot auto-create repos. I will create an
  empty GitHub repo manually; Axon builds the URL from a username in `.env`.
  `axon approvals` must show the exact `git push` command before I approve — no surprises.
- **`claude` CLI IS installed** (2.1.160). Non-interactive via `-p`, structured output
  via `--output-format json`, tool restriction via `--allowedTools`.
- Generated projects go in `./projects/<note-id>-<name>/` — real deliverables, kept
  separate from Axon's internal `data/` folder. Gitignore it.
- `axon add` will become slow (minutes, not seconds) while a builder works. Show progress.

---

## 6. Later (V3+)

More hands, one at a time: email, Teams/Slack, WhatsApp/SMS, browser/shopping, files and
docs, media creation, research and summarise, query data. Wire the easy ones through
**n8n** (self-hosted, free). Phone calls and IoT much later.

---

## 7. The ruflo agents

11 agent files live in `.claude/agents/` (core, swarm, testing, plus python-specialist).
They load natively as Claude Code agents — free, no MCP server needed. The inert
`mcp__claude-flow__*` sections in some of them are harmless no-ops.

**Do not run `production-validator` on this project.** It insists no mocks remain in the
codebase. Axon's mock brain and mock builder are *required product features* — they are
what make it run for free with no key. This exception is recorded in
[docs/SPEC.md](SPEC.md).

Note that `core/coder.md` reads `docs/SPEC.md` and `docs/adr/*.md` before implementing
and treats ADRs as **binding**. That is intentional — it is what stops an agent quietly
swapping the stack.

## 8. Start here

Read this file, confirm you understand the state, then **propose Step 7 and wait for my
go-ahead before writing code.**
