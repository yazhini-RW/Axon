# Axon — Status

**Last updated: 2026-08-10.** Point a new session here for "where is this project?"
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
| Web UI | Plain HTML/CSS/JS in one file — two-column dashboard, aurora background, staggered animations. No framework, no build step, works offline. |
| Smart brain (optional) | Google Gemini, via `agent-framework-openai`'s `OpenAIChatCompletionClient` against Gemini's OpenAI-compatible endpoint |
| Hands | `git` (shelled out), `smtplib` (stdlib, email), `httpx` (Slack/Teams webhook, WhatsApp Cloud API, DuckDuckGo) |
| Project builder | `MockBuilder` (free, default) or `ClaudeCodeBuilder` (spawns the `claude` CLI, opt-in via `AXON_BUILDER=claude`) |
| Tests | pytest + pytest-asyncio — 227 tests (216 fast, 11 slow) |
| Config | `python-dotenv` reading `.env` |
| Deployment | Render free tier, config in [`render.yaml`](../render.yaml) |

---

## 1. Remaining work

| Item | Status |
| --- | --- |
| **Step 12 — real Claude Code builder** | ✅ Built and wired, ❌ **never run for real.** Off by default. |
| **Email hand — real verification** | ✅ **Done** (2026-08-06). Real email sent through the full workflow and confirmed received. |
| **WhatsApp hand (Step 17)** | ✅ **Done** (2026-08-10). Real message sent via Meta's Cloud API through the full workflow and confirmed received. |
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

## 6. WhatsApp hand (Step 17)

Uses Meta's Cloud API on the free test-number tier — a test sender Meta provides,
messaging only the ~5 numbers verified in the Meta dashboard. Same two-half shape as
email and chat: `prepare()` drafts locally and costs nothing, `execute()` makes the one
real API call and only runs after approval.

Business-initiated WhatsApp messages cannot be arbitrary text — they must go through an
approved template, so `execute()` always sends one (`WHATSAPP_TEMPLATE` in `.env`,
default `axon_note`), with the note's text passed as a `{{1}}` body parameter. The
template created for this project: category Utility, body
`Axon: {{1}} (sent from your second brain)`.

Auth uses a **System User token** (Business Settings → Users → System users), not the
24-hour dashboard token — generated with `whatsapp_business_messaging` and
`whatsapp_business_management` permissions, 60-day expiry. One non-obvious step: the
system user also needed an explicit **app role** (Settings → Accounts → Apps → Axon →
add `axon-sender` → "Test app") before the token dialog would offer any permissions to
select at all — without that role the permission list is simply empty.

Routing matches only explicit "whatsapp"/"wa" mentions, deliberately narrower than the
other hands: "message me" or "text me" reads just as naturally as SMS, and a wrong guess
here puts a real message on a real phone rather than just misfiling a note.

## 7. Web UI

One file, [`axon/web/static/index.html`](../axon/web/static/index.html) — inline CSS and
vanilla JS calling the same `/api/*` routes the CLI calls in-process. Deliberately no npm
and no build step (V2-PLAN Step 11); Next.js was considered on 2026-08-06 and rejected
for that reason — "better UI" was a design problem, not a framework one.

Layout: main column (Capture + Notes), sticky sidebar (Approvals + Recall), collapsing to
one column under 940px. Light/dark via `prefers-color-scheme` with a `data-theme`
override, and ambient motion drops out under `prefers-reduced-motion`.

Two details that carry meaning rather than decoration:

- **Recall score bars.** A proportional bar under each hit makes "0.59 vs 0.58" read as
  the near-tie it is — the bare numbers hide that, and it's the same honesty the
  "not confident which you meant" line already tries to convey.
- **Example chips under the capture box.** They fill the input rather than submitting.
  They exist because of a real question asked while demoing: *"where is the research
  button?"* There isn't one — every kind of note goes through the same box, and watching
  an example land there is what makes that legible.

---

## 8. Bugs found and fixed on 2026-08-06

- **`win11toast` broke the Linux build.** It pulls in the `winrt` native extension, which
  has no Linux wheels, so Render's build failed outright. The code already degraded to
  console output when it's missing, so this was purely a dependency-list bug — now gated
  behind `sys_platform == "win32"` in `requirements.txt`.
- **Gmail app passwords crashed SMTP login.** Google *displays* them as
  `abcd efgh ijkl mnop` using non-breaking spaces (`\xa0`), so pasting one as shown
  surfaced as a `UnicodeEncodeError` deep inside `smtplib` rather than anything
  resembling "your password has spaces in it". `axon/config.py` now strips all
  whitespace from `EMAIL_APP_PASSWORD`.
