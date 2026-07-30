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
| `axon approvals` | show pending approvals |
| `axon approve <id>` / `axon reject <id>` | the human checkpoint |
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

## Known limits (measured, not guessed)

- **Embedding recall is imperfect.** In verification, the query *"how do I get online"*
  matched *"deploy the staging server every Friday"* (0.615) instead of the wifi-password
  note. This is why acceptance criterion 4 requires ranked top-k with visible scores
  rather than one confident answer. A larger model (`thenlper/gte-large`, ~670MB) scores
  better and is opt-in via config; the default is `BAAI/bge-small-en-v1.5` (~130MB).
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
  to 09:00 *today*. Step 4 must decide what to do with an already-due reminder (fire
  immediately, or warn) rather than scheduling a job that can never run.
- **Recurring notes fire once in V1.** "every Friday" is detected and flagged, and only
  the next occurrence is scheduled. Real recurrence is a later step, and Axon says so
  at capture time rather than silently promising weekly behaviour.
- **Windows notifications are Windows-only.** Elsewhere Axon prints to console.
- **Checkpoints are never cleaned up yet.** Every `axon add` writes one JSON file per
  workflow superstep to `data/checkpoints/`, and nothing deletes them. Step 5 owns the
  fix, because the retention rule depends on approvals: a checkpoint can only be deleted
  once its run finished with no pending requests. Until then, disk use grows slowly.

## Deliberate exception for the `production-validator` agent

The bundled `production-validator` agent demands that no mocks remain in the codebase.
**That does not apply to Axon's mock LLM.** The rule-based mock brain is a required
product feature — it is what makes Axon run for free with no key. It is not placeholder
scaffolding and must not be "cleaned up".
