# Axon — Status

**Last updated: 2026-08-06.** Point a new session here for "where is this project?"
before reading anything else. For *why* each step is the way it is, read the commit
messages (`git log`, not `--oneline`) — they carry the reasoning. For the original build
plan, see [V2-PLAN.md](V2-PLAN.md).

---

## Tech stack

| Layer | What |
| --- | --- |
| Language | Python 3.11 |
| Workflow engine | **Microsoft Agent Framework** — `classify → remember → prepare → gate → execute → persist`, with durable pause/resume across processes |
| Notes storage | SQLite (WAL mode) |
| Long-term memory | Qdrant (local, on-disk) + Mem0 + fastembed (local embeddings, no key) |
| Scheduling | APScheduler + `dateparser` |
| CLI | Typer + Rich |
| Web backend | FastAPI + Uvicorn |
| Web UI | Plain HTML/CSS/JS, two-column dashboard — no framework, no build step |
| Smart brain (optional) | Google Gemini, via `agent-framework-openai`'s `OpenAIChatCompletionClient` against Gemini's OpenAI-compatible endpoint |
| Hands | `git` (shelled out), `smtplib` (stdlib, email), `httpx` (Slack/Teams webhook + DuckDuckGo) |
| Project builder | `MockBuilder` (free, default) or `ClaudeCodeBuilder` (spawns the `claude` CLI, opt-in via `AXON_BUILDER=claude`) |
| Tests | pytest + pytest-asyncio — 206 tests (195 fast, 11 slow) |
| Config | `python-dotenv` reading `.env` |
| Deployment | Render free tier, config in [`render.yaml`](../render.yaml) |

---

## 1. Remaining work

| Item | Status |
| --- | --- |
| **Step 12 — real Claude Code builder** | ✅ Built and wired, ❌ **never run for real.** Off by default. |
| **Email hand — real verification** | ✅ **Done** (2026-08-06). Real email sent through the full workflow and confirmed received. |
| **Chat hand — real verification** | ❌ Built and unit-tested against a *mock* webhook. Never posted to a real Slack/Teams. Needs a real `CHAT_WEBHOOK_URL`, then one real post → approve → confirm. |

### 1b. Prerequisites before Step 12 can be tested

All three are deliberate gates, not oversights — approving a build spends real Claude
Pro usage, so nothing happens until these are set explicitly.

| Item | Status |
| --- | --- |
| `GITHUB_USERNAME` in `.env` | ❌ Not set — `execute()` fails fast here, spending nothing |
| `AXON_BUILDER=claude` in `.env` | ❌ Not set — defaults to the free `mock` builder |
| Empty GitHub repo created manually | ❌ `gh` isn't installed, so Axon can't create it. Name must match `<note_id>-<slug>`, e.g. `3-todo-app` |

**How Step 12 spends usage:** only inside `GitHubHand.execute()`, i.e. *after* a human
approves. `prepare()` always uses the free `MockBuilder`, so drafting and rejecting cost
nothing. It draws on the Claude Pro plan's shared allowance — the same meter as ordinary
Claude Code work, not a separate bill.

---

## 2. Not decided yet (parked, **not** rejected)

These were once written up as permanent skips. That was wrong — the user has not decided
against any of them. Ask before building *or* before continuing to skip.

| Item | Why it's parked |
| --- | --- |
| **WhatsApp/SMS** | No genuinely free path found (Twilio's trial needs phone verification and has limits). May still be wanted, possibly accepting a cost. |
| **Media creation** (images/video) | No meaningful free/local generator found. Same caveat. |
| **Browser/shopping** | No concrete site named to automate, and "pay" needs careful design. |
| **Query data** | *Believed* already covered by `axon recall`, but that was an agent judgment call, never confirmed. |

---

## 3. Feature ideas (researched 2026-08-04, nothing started)

| Idea | Effort |
| --- | --- |
| **Auto-linking related notes** — "this connects to note #3" | Low — reuses the existing embeddings |
| **Daily/weekly digest** — "here's what you captured this week" | Low — reuses Gemini |
| **Voice capture** — speak instead of type (local Whisper) | Medium |
| **Tagging/auto-fields** — `#meeting` auto-extracts date/people/action items | Medium |
| **Browser clipper** — bookmarklet to save a webpage snippet | Medium — a far simpler answer to the parked "browser" item |

---

## 4. Deployment

Live at **https://axon-23eh.onrender.com** (repo: `github.com/yazhini-RW/Axon`).

| Item | Status |
| --- | --- |
| Web UI + API | ✅ Live |
| Gemini brain | ✅ Enabled via Render dashboard env vars |
| Reminders (`axon run`) | ❌ Free tier runs one process per service and sleeps after ~15 min idle. Run the daemon locally instead. |
| Persistent data | ❌ Ephemeral — persistent disk is paid ($0.25/GB/mo). Deployed notes reset on sleep/restart. |

Neither ❌ is a bug; both are free-tier limits. Treat the deployed link as a live demo of
the UI and workflow, not as a store for real notes.

**Secrets are set in Render's dashboard, never committed.** Email/chat/GitHub credentials
are deliberately *not* set there — the URL is public, and anyone finding it could
otherwise trigger real sends from your accounts.

---

## 5. Running it

| Goal | Command |
| --- | --- |
| Local web UI (real notes) | `axon serve` → http://127.0.0.1:8420 |
| Local demo sandbox | `.\demo.ps1` (separate `.\demo\data`, safe to reset) |
| Reminder daemon (demo sandbox) | `.\demo-run.ps1` — second terminal, required for reminders to fire |
| Reset the demo sandbox | `.\demo-reset.ps1` |
| Deployed link | Nothing to run — just open the URL. Wake it ~1 min early; it sleeps. |

---

## 6. Bugs found and fixed on 2026-08-06

- **`win11toast` broke the Linux build.** It pulls in the `winrt` native extension, which
  has no Linux wheels, so Render's build failed outright. The code already degraded to
  console output when it's missing, so this was purely a dependency-list bug — now gated
  behind `sys_platform == "win32"` in `requirements.txt`.
- **Gmail app passwords crashed SMTP login.** Google *displays* them as
  `abcd efgh ijkl mnop` using non-breaking spaces (`\xa0`), so pasting one as shown
  surfaced as a `UnicodeEncodeError` deep inside `smtplib` rather than anything
  resembling "your password has spaces in it". `axon/config.py` now strips all
  whitespace from `EMAIL_APP_PASSWORD`.
