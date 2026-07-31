# Axon

A personal "second brain" that remembers what you tell it **and acts on it** — and that
always stops to ask before doing anything risky.

Notes don't just sit in a list. Axon works out what each one *is* (a fact worth keeping,
a task, a timed reminder), stores it so you can ask about it later in your own words, and
does something about it at the right time.

```
axon add "remind me to buy milk at 5pm"
axon recall "groceries"        ->  buy milk at 5pm
axon run                       ->  a real notification at 17:00
```

**It costs nothing to run.** No paid service and no API key. With no key set, Axon uses a
rule-based brain and local embeddings, fully offline.

## Status

🚧 Early. Building V1 one step at a time.

- [x] **Step 0** — skeleton, pinned deps, specification, architecture decisions
- [x] **Step 1** — capture notes (`axon add`, `axon list`)
- [x] **Step 2** — the brain classifies each note
- [x] **Step 3** — memory and meaning-based `axon recall`
- [x] **Step 4** — scheduler and notifications (`axon run`)
- [x] **Step 5** — the human approval pause (`axon approvals`, `axon approve`, `axon reject`)
- [x] **Step 6** — optional Gemini brain (set `GEMINI_API_KEY` to use it)

**V1 is complete.**

V2 adds the first real "hand": build code and push to GitHub, pausing for approval before
the push. V3+ adds more hands — email, chat, browser, files, research.

## How it works

| Part | Built with |
|---|---|
| The brain — steps, branching, approval pauses | Microsoft Agent Framework |
| Thinking | rule-based mock by default; Google Gemini free tier optional |
| Long-term memory | Mem0 |
| Meaning-based search | Qdrant (local) + fastembed (local) |
| Tasks, reminders, status | SQLite |
| "at 5pm", "in 5 days" | APScheduler + dateparser |
| Interface | Typer CLI |

The interesting engineering is the **approval checkpoint**: when Axon reaches a risky
step it writes its entire state to disk and stops. You can close the terminal, reboot,
come back tomorrow and run `axon approve 3` — a fresh process picks the workflow up
exactly where it left off. It is not an `input()` prompt.

## Setup

Requires Python 3.11+ on Windows (other platforms work; notifications fall back to
console output).

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

No `.env` needed. To use Gemini later, copy `.env.example` to `.env` and add a free key.

First run downloads a ~130MB embedding model once, then never needs the network again.

## Documentation

- [docs/SPEC.md](docs/SPEC.md) — what V1 does, and its acceptance criteria
- [docs/adr/](docs/adr/) — why the stack is what it is. **These are binding**: they exist
  so the architecture doesn't drift. Read before changing anything structural.
