# Axon demo script

Run `.\demo.ps1` from `c:\Axon`, open http://127.0.0.1:8420, then work through these
in order. `.\demo-reset.ps1` clears the demo folder if you want to practice again from
scratch — never touches your real notes.

| # | Type in the note box | What it shows |
|---|---|---|
| 1 | `the wifi password is on the whiteboard` | A **fact** — remembered instantly, nothing to approve |
| 2 | `buy milk at 5pm` | A **reminder** with a real time — but also risky ("buy"), so it pauses for your OK first |
| 3 | `fix the login bug` | A **task** — no time, no money/contact involved |
| 4 | `build a github repo for a todo app` | **GitHub hand** — click Approve, then check `demo\projects\<id>-todo-app\` for a real commit made before you even approved |
| 5 | `save my grocery list to groceries.txt` | **Files hand** — writes a real file to `demo\documents\` |
| 6 | `what is the eiffel tower` | **Research hand** — a real, live answer appears immediately, no approval needed (read-only) |
| 7 | `tell the team the deploy is done` | **Chat hand** — drafts a message, pauses for approval (won't actually post unless `CHAT_WEBHOOK_URL` is set in `.env`) |
| 8 | `email sarah@example.com about the invoice` | **Email hand** — drafts a message, pauses for approval (won't actually send unless `EMAIL_ADDRESS`/`EMAIL_APP_PASSWORD` are set) |

Then, in the **Approve/Reject** step for #2 or #4:
- Click **Approve** on one, **Reject** on another — show both outcomes land correctly
  in the Notes list (`classified` vs `blocked`).

Finally, in the **Recall** box:
- Search `how do I connect to the internet` → finds note #1 by meaning, not exact words.
- Search `what should I get from the store` → finds note #5.

### The one sentence that sells it

"Axon never does anything risky — spending money, sending a message, pushing code —
without stopping and asking me first. Everything else, it just handles."
