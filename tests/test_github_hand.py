"""The GitHub hand's safe half: build + local commit, never a push. See
docs/V2-PLAN.md Step 8.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axon.config import Settings
from axon.hands import pick_hand
from axon.hands.github import GitHubHand, looks_like_a_github_note, slugify
from axon.hands.noop import NoopHand
from axon.models import Classification, ClassifiedNote, NoteKind


def _note(text: str, note_id: int = 1) -> ClassifiedNote:
    return ClassifiedNote(
        note_id=note_id, text=text,
        classification=Classification(kind=NoteKind.TASK, risky=True, reason="test"),
    )


# --- routing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "build a github repo for my todo app",
        "push the axon repo to github",
        "create a new repository on github for this",
        "set up a github repo",
    ],
)
def test_recognises_github_build_notes(text: str) -> None:
    assert looks_like_a_github_note(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "my github repo is private",  # fact, no build verb
        "fix the login bug",
        "buy milk at 5pm",
    ],
)
def test_does_not_misroute_ordinary_notes(text: str) -> None:
    assert looks_like_a_github_note(text) is False


def test_pick_hand_routes_github_notes_to_github_hand(settings: Settings) -> None:
    assert isinstance(pick_hand("build a github repo for a todo app", settings), GitHubHand)


def test_pick_hand_routes_everything_else_to_noop(settings: Settings) -> None:
    assert isinstance(pick_hand("fix the login bug", settings), NoopHand)


def test_slugify_produces_a_short_filesystem_safe_name() -> None:
    slug = slugify("build a github repo for my todo app")
    assert slug == "todo-app"
    assert "/" not in slug and " " not in slug


def test_slugify_never_returns_empty() -> None:
    assert slugify("github repo") == "project"


# --- prepare: real files, real commit, never a push --------------------------------


async def test_prepare_writes_real_files_and_commits_them(settings: Settings) -> None:
    hand = GitHubHand(settings=settings)
    note = _note("build a github repo for a todo app", note_id=7)

    prepared = await hand.prepare(note)

    assert prepared.project_dir is not None
    project_dir = Path(prepared.project_dir)
    assert (project_dir / "README.md").exists()
    assert (project_dir / "main.py").exists()
    assert (project_dir / ".git").is_dir()
    assert "committed" in prepared.detail


async def test_prepare_leaves_a_real_git_log_entry(settings: Settings) -> None:
    import subprocess

    hand = GitHubHand(settings=settings)
    note = _note("build a github repo for a todo app", note_id=8)

    prepared = await hand.prepare(note)

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=prepared.project_dir,
        capture_output=True, text=True, check=True,
    )
    assert "Axon:" in log.stdout


async def test_prepare_never_configures_a_remote_or_pushes(settings: Settings) -> None:
    """Step 8 is the safe half only. No remote, no push, no network."""
    import subprocess

    hand = GitHubHand(settings=settings)
    note = _note("build a github repo for a todo app", note_id=9)

    prepared = await hand.prepare(note)

    remotes = subprocess.run(
        ["git", "remote"], cwd=prepared.project_dir,
        capture_output=True, text=True, check=True,
    )
    assert remotes.stdout.strip() == "", "prepare must never add a remote"


async def test_prepare_scopes_the_project_dir_to_settings(settings: Settings) -> None:
    """Regression: an earlier version silently fell back to the real ./projects
    because GitHubHand() built with no settings, ignoring the caller's projects_dir."""
    hand = GitHubHand(settings=settings)
    note = _note("build a github repo for a todo app", note_id=10)

    prepared = await hand.prepare(note)

    assert Path(prepared.project_dir).is_relative_to(settings.projects_dir)


async def test_execute_is_a_noop_in_step_8(settings: Settings) -> None:
    """Step 9 adds the real push. For now execute must do nothing at all."""
    from axon.models import ApprovalOutcome

    hand = GitHubHand(settings=settings)
    note = _note("build a github repo for a todo app", note_id=11)
    prepared = await hand.prepare(note)

    result = await hand.execute(ApprovalOutcome(note=note, approved=True))
    assert result is None
