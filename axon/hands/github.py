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

import re
import subprocess
from pathlib import Path

from axon.brain.classifier import first_word
from axon.config import Settings, get_settings
from axon.hands.builders.base import Builder
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


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitCommandError(f"git {' '.join(args)} failed: {detail}")


class GitHubHand:
    """`prepare`: build + local commit. `execute`: real push, only once approved."""

    def __init__(self, builder: Builder | None = None, settings: Settings | None = None) -> None:
        self._builder = builder or MockBuilder()
        self._settings = settings or get_settings()

    def _project_dir(self, note: ClassifiedNote) -> Path:
        name = f"{note.note_id}-{slugify(note.text)}"
        return self._settings.projects_dir / name

    def _push_url(self) -> str | None:
        if not self._settings.github_username:
            return None
        # The repo name matches the project folder name; the human creates it manually
        # on GitHub with this same name before approving. See "Practical constraints".
        return f"git@github.com:{self._settings.github_username}/<repo-name>.git"

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        project_dir = self._project_dir(note)
        project_dir.mkdir(parents=True, exist_ok=True)

        files = self._builder.build(note, project_dir)

        _run_git(["init"], cwd=project_dir)
        _run_git(["add", *files], cwd=project_dir)
        _run_git(
            ["-c", "user.email=axon@localhost", "-c", "user.name=Axon",
             "commit", "-m", f"Axon: {note.text}"],
            cwd=project_dir,
        )

        return PreparedNote(
            note=note,
            detail=f"committed {len(files)} file(s) to {project_dir}",
            project_dir=str(project_dir),
            push_url=self._push_url(),
        )

    async def execute(self, outcome: ApprovalOutcome) -> None:
        """Step 9 will push here. For now, prepare's commit is the entire effect."""
        return None
