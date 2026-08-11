"""The GitHub hand: build + commit locally (safe), push only on approval (risky).

See docs/V2-PLAN.md Steps 8-9. `prepare` never touches the network — it writes real
files with a Builder (default: MockBuilder, free) and makes a real local commit with
`git init / add / commit`. `execute` (Step 9) is the only place a push can happen, and
only runs when the human has approved.

`gh` (GitHub CLI) is not installed on this machine — Axon cannot create the remote repo.
The human creates an empty repo manually; Axon only ever builds the push URL from
`GITHUB_USERNAME` in .env. See docs/V2-PLAN.md "Practical constraints".
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from axon.brain.classifier import first_word
from axon.config import Settings, get_settings
from axon.hands.builders.base import Builder, get_builder
from axon.hands.builders.mock import MockBuilder
from axon.models import ApprovalOutcome, ClassifiedNote, PreparedNote

# A note routes to GitHubHand only if it mentions GitHub/a repo AND reads like an
# action (build/push/create/...), not e.g. a fact like "my github repo is private".
_GITHUB_WORDS = {"github", "repo", "repository"}
_BUILD_VERBS = {"build", "create", "make", "push", "commit", "start", "setup"}
_BUILD_PHRASES = ("set up",)


def looks_like_a_github_note(text: str) -> bool:
    lowered = text.lower()
    words = set(re.findall(r"[a-z]+", lowered))
    has_build_signal = (
        bool(words & _BUILD_VERBS)
        or first_word(text) in _BUILD_VERBS
        or any(phrase in lowered for phrase in _BUILD_PHRASES)
    )
    return bool(words & _GITHUB_WORDS) and has_build_signal


def slugify(text: str) -> str:
    """A short filesystem-safe name for the project folder, e.g. 'hello-world'."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    skip = (
        _GITHUB_WORDS | _BUILD_VERBS
        | {"the", "a", "an", "to", "for", "and", "on", "my", "this", "set", "up"}
    )
    slug = "-".join(w for w in words if w not in skip)[:40].strip("-")
    return slug or "project"


class GitCommandError(RuntimeError):
    """A git subprocess exited non-zero. Message includes stderr for debugging."""


def _run_git_sync(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


async def _git(args: list[str], cwd: Path) -> str:
    """Run git off the event loop thread — prepare/execute run inside the workflow's
    own asyncio loop (see workflow.py's module docstring), and a blocking subprocess
    call there would stall every other note. Returns stdout on success."""
    result = await asyncio.to_thread(_run_git_sync, args, cwd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitCommandError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


class GitHubHand:
    """`prepare`: build + local commit. `execute`: real push, only once approved.

    `builder` (prepare-time) is always MockBuilder, regardless of AXON_BUILDER — drafting
    a note must never cost anything (Step 12). `real_builder` (execute-time) is what
    AXON_BUILDER actually selects: MockBuilder again unless the human opted into
    AXON_BUILDER=claude, in which case approving this note spends real Claude usage.
    """

    def __init__(
        self,
        builder: Builder | None = None,
        real_builder: Builder | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._builder = builder or MockBuilder()
        self._real_builder = real_builder or get_builder(self._settings)

    def _repo_name(self, note: ClassifiedNote) -> str:
        return f"{note.note_id}-{slugify(note.text)}"

    def _project_dir(self, note: ClassifiedNote) -> Path:
        return self._settings.projects_dir / self._repo_name(note)

    def _push_url(self, note: ClassifiedNote) -> str | None:
        if not self._settings.github_username:
            return None
        # The repo name matches the project folder name; the human creates it manually
        # on GitHub with this same name before approving. See "Practical constraints".
        return f"git@github.com:{self._settings.github_username}/{self._repo_name(note)}.git"

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        project_dir = self._project_dir(note)
        project_dir.mkdir(parents=True, exist_ok=True)

        files = self._builder.build(note, project_dir)

        await _git(["init"], cwd=project_dir)
        await _git(["add", *files], cwd=project_dir)
        await _git(
            ["-c", "user.email=axon@localhost", "-c", "user.name=Axon",
             "commit", "-m", f"Axon: {note.text}"],
            cwd=project_dir,
        )

        detail = f"committed {len(files)} file(s) to {project_dir}"
        if self._real_builder.name == "claude":
            # Step 12's usage warning: shown on the approval screen, before anything is
            # spent — approving is the moment this stub gets replaced by a real Claude
            # Code build, which draws on your Pro plan's shared usage, not a free action.
            detail += (
                " (draft only — approving will run a REAL Claude Code build here, "
                "using your Claude Pro plan's usage)"
            )

        return PreparedNote(
            note=note,
            detail=detail,
            project_dir=str(project_dir),
            push_url=self._push_url(note),
        )

    async def execute(self, outcome: ApprovalOutcome) -> None:
        """The risky half. Only ever called with an approved outcome — see
        ExecuteExecutor.run_execute in axon/brain/workflow.py, which checks
        outcome.approved before calling any hand's execute() at all.

        Re-derives project_dir and push_url from the note rather than trusting anything
        carried in-memory from prepare(), because this can run in a fresh process after
        a resume (same reason PrepareExecutor/ExecuteExecutor take a HandResolver, not a
        fixed Hand — see Step 8's commit message).

        Step 22: a real Claude Code build stops here, after the local commit, and does
        NOT push. axon.service creates a second, separate approval right after this
        returns (see PreparedNote/ApprovalOutcome's detail field, which this leaves as
        the signal), so the human can look at project_dir on disk before the code ever
        leaves the machine. The free mock builder has nothing worth a second look at,
        so it still pushes immediately here, unchanged from before Step 22.
        """
        note = outcome.note
        project_dir = self._project_dir(note)
        push_url = self._push_url(note)

        if push_url is None:
            raise RuntimeError(
                "GITHUB_USERNAME is not set in .env — Axon doesn't know where to push. "
                f"The commit is safe at {project_dir}; set GITHUB_USERNAME and re-approve."
            )

        if self._real_builder.name == "claude":
            # Only reachable once approved (ExecuteExecutor checks outcome.approved
            # before calling this at all) -- this is the one place Step 12 spends real
            # Claude usage. Off the event loop thread for the same reason as the git
            # calls: it can take minutes, and this shares the workflow's own loop.
            await asyncio.to_thread(self._real_builder.build, note, project_dir)
            await _git(["add", "-A"], cwd=project_dir)
            await _git(
                ["-c", "user.email=axon@localhost", "-c", "user.name=Axon",
                 "commit", "-m", f"Axon (Claude Code): {note.text}"],
                cwd=project_dir,
            )
            return  # push is gated separately -- see docstring

        await push_project(project_dir, push_url)


async def push_project(project_dir: Path, push_url: str) -> None:
    """The actual `git push`, on its own so the Step 22 push-approval path (in
    axon.service) can call exactly this and nothing else -- no build, no commit,
    just what "approve the push" means."""
    existing_remotes = await _git(["remote"], cwd=project_dir)
    if "origin" not in existing_remotes.split():
        await _git(["remote", "add", "origin", push_url], cwd=project_dir)

    branch = (await _git(["branch", "--show-current"], cwd=project_dir)).strip() or "master"

    await _git(["push", "-u", "origin", branch], cwd=project_dir)
