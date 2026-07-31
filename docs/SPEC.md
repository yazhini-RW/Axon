# Axon — Specification (V1)

Axon is a personal "second brain" that remembers what you tell it **and acts on it**.
Notes don't just sit in a list — Axon works out what each one *is*, stores it so you can
ask about it later in your own words, and does something about it at the right time.

The design rule that matters most: **Axon always stops and asks before doing anything
risky** (paying, sending, pushing, deploying). It never takes a risky action on its own.

---

## Scope of V1

V1 builds the brain, the memory, and the scheduler — end to end, but small. No email,
no browser, no GitHub yet. Those are V2+.

### The one flow V1 must nail

```
axon add "remind me to buy milk at 5pm"
   │
   ├─ 1. CAPTURE   note written to SQLite immediately, before anything can fail
   ├─ 2. CLASSIFY  fact / task / timed-reminder            (mock rules, or Gemini)
   ├─ 3. EXTRACT   if timed: resolve the actual datetime
   ├─ 4. REMEMBER  note embedded and stored for meaning-based search
   ├─ 5. CHECKPOINT if the note is risky, PAUSE and record a pending approval
   └─ 6. SCHEDULE  persist an APScheduler job

axon run                  daemon; at 17:00 a real Windows notification appears
axon recall "groceries"   finds "buy milk at 5pm" — no shared word
axon approvals            lists what's waiting for you
axon approve <id>         releases the paused step
```

### Commands in V1

| Command | Does |
|---|---|
| `axon add "<note>"` | capture → classify → remember → schedule (the whole flow) |
| `axon list` | show notes and reminders with their status |
| `axon recall "<query>"` | meaning-based search over memory, ranked with scores |
| `axon run` | start the scheduler; fires due reminders as notifications |
| `axon approvals` | show what's waiting on your OK |
| `axon approve <id>` | give the OK to a paused, risky action |
| `axon reject <id>` | say no — Axon will not do it |
| `axon doctor` | print mode (mock vs Gemini), DB path, memory status |

---

## Acceptance criteria

V1 is done when all of these are true:

1. `axon add "buy milk at 5pm"` classifies as a timed reminder and resolves 17:00 today.
2. `axon add "we chose Microsoft Agent Framework because it's newest"` classifies as a
   fact, not a task.
3. `axon recall "groceries"` returns the milk note, **which shares no word with the query**.
4. `axon recall` returns ranked results with visible scores — never a single answer
   presented as certain. (Measured reason: see Known limits.)
5. `axon run` fires a due reminder as a Windows notification **and** a console line.
6. A reminder scheduled before a restart still fires after the process is killed and
   `axon run` is started again.
7. `axon add "push the axon repo to github"` does **not** act. It pauses, and
   `axon approvals` shows it waiting.
8. `axon approve <id>` resumes that paused work **in a new process** and completes it.
9. `axon reject <id>` resumes it and records that it was blocked.
10. Every one of the above works with **no API key set** and **no network** (after the
    one-time embedding-model download).
11. `pytest` passes offline with no keys.

---

## Hard constraints

- **Free only.** No paid service, no paid API key, ever. If a key is absent, Axon runs
  in mock mode and every command still works.
- **Local only.** Nothing is pushed or deployed. No remote git.
- **Human approval is not optional.** Risky work pauses. See
  [ADR-0003](adr/0003-human-approval-checkpoint.md).
- **One feature at a time.** Each build step ends with something that actually runs.

---

## Step 6: the optional Gemini brain

With `GEMINI_API_KEY` set, `axon add` uses Google Gemini (free tier) instead of the mock
brain — same interface, same output shape, no other command changes. See
[ADR-0005](adr/0005-gemini-brain.md) for how, and its Limits section for what has and
has not actually been run against Google's servers (no key was available in this
session; only the stubbed-client path is tested).

## Known limits (measured, not guessed)

- **Embedding recall is imperfect, and scores bunch up.** Real measured scores sit in a
  narrow 0.4-0.7 band, so an *absolute* cutoff is useless: at a 0.45 threshold the
  correct answer to *"which agent library did I pick"* (0.42) was hidden, while the
  wrong answer to *"how do I get online"* (0.59) was shown confidently. `axon recall`
  therefore ranks results **against each other** — it shows everything within 0.15 of
  the best, and when first and second place are within 0.05 it says outright that it
  is not confident. A larger model (`thenlper/gte-large`, 1.2GB) scores better and is
  opt-in via `AXON_EMBED_MODEL`; the default is `BAAI/bge-small-en-v1.5` (134MB).
- **Mem0 builds an LLM at construction time** even when it is never used. In mock mode
  Axon passes a placeholder key and only ever calls `add(infer=False)`, so no LLM is
  invoked. See [ADR-0002](adr/0002-free-local-only-stack.md).
- **Natural-language time parsing is approximate.** In mock mode a rule-based parser
  plus `dateparser` handles common phrasings ("at 5pm", "in 5 days", "tomorrow morning").
  Unusual phrasings may fail; Axon says so rather than guessing silently.
- **`dateparser` must never be given raw text.** It parses the bare words "we", "to" and
  "on" as valid dates, which would turn every fact into a reminder. A strict regex finds
  genuine time expressions first and only those fragments are parsed. There is a test
  pinning this (`test_bare_prepositions_are_never_read_as_dates`) — do not relax it.
- **A resolved time can land in the past.** "call mum this morning" typed at 6pm resolves
  to 09:00 *today*. `axon add` warns when this happens. The daemon then fires anything
  due within the last 24 hours immediately, marked *(overdue)*, and marks anything older
  as `missed` without notifying — see [ADR-0004](adr/0004-scheduler-design.md).
- **Recurring notes fire once in V1.** "every Friday" is detected and flagged, and only
  the next occurrence is scheduled. Real recurrence is a later step, and Axon says so
  at capture time rather than silently promising weekly behaviour.
- **Windows notifications are Windows-only.** Elsewhere Axon prints to console.
- **Checkpoint cleanup (fixed in Step 5).** Every workflow run still writes a checkpoint
  per superstep, but after each `axon add` / `axon approve` / `axon reject`, everything
  not referenced by a currently-pending approval is deleted. Verified: 0 files remain on
  disk after both an approve and a reject.

## Deliberate exception for the `production-validator` agent

The bundled `production-validator` agent demands that no mocks remain in the codebase.
**That does not apply to Axon's mock LLM.** The rule-based mock brain is a required
product feature — it is what makes Axon run for free with no key. It is not placeholder
scaffolding and must not be "cleaned up".
