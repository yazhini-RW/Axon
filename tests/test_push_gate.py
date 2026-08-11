"""Step 22: a real Claude Code build stops at the local commit; pushing to GitHub is a
second, separate approval so the human can look at the code on disk first.

Uses the same local-bare-repo-standing-in-for-GitHub technique test_github_hand.py
already established, plus a fake builder (never invokes the real `claude` CLI, never
spends anything) standing in for ClaudeCodeBuilder -- this suite is about the two-gate
sequencing, not about Claude Code itself.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from axon import service
from axon.brain.workflow import run_capture
from axon.config import Settings
from axon.db import repo
from axon.models import Classification, ClassifiedNote, NoteKind


class _FakeClaudeBuilder:
    """Writes one real file, instantly, no subprocess -- stands in for the real
    ClaudeCodeBuilder so these tests never shell out to `claude` or spend anything."""

    name = "claude"

    def build(self, note: ClassifiedNote, project_dir: Path) -> list[str]:
        (project_dir / "app.py").write_text("print('built by the fake claude builder')\n")
        return ["app.py"]


def _configured(settings: Settings, bare_repo: Path) -> Settings:
    return replace(settings, github_username="octocat", axon_builder="claude")


def _bare_repo(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    return bare


async def _paused_github_build(settings: Settings, text: str = "push a github repo for a todo app") -> int:
    """A real github-build note, really paused at the gate, with a matching approvals
    row -- same pattern test_scheduled_approvals.py's _paused established."""
    with repo.open_db(settings) as conn:
        note = repo.add_note(conn, text)
        capture = await run_capture(note.id, note.text, lambda _o: None, settings=settings)
        assert capture.pending is not None
        return repo.create_approval(
            conn, note.id, capture.pending.request_id, capture.pending.checkpoint_id,
            capture.pending.request.action,
            detail=capture.pending.request.detail,
            project_dir=capture.pending.request.project_dir,
            push_url=capture.pending.request.push_url,
        )


def _patch_real_builder(hand_resolver_settings: Settings, bare_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Points GitHubHand's real builder at the fake one, and its push URL at the local
    bare repo instead of a real GitHub URL -- same reasoning
    test_execute_pushes_to_the_configured_remote already relies on: execute() re-derives
    both from the note each time rather than trusting anything from prepare()."""
    import axon.hands.github as github_module

    monkeypatch.setattr(github_module.GitHubHand, "_push_url", lambda self, note: str(bare_repo))
    original_init = github_module.GitHubHand.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._real_builder = _FakeClaudeBuilder()

    monkeypatch.setattr(github_module.GitHubHand, "__init__", patched_init)


# --- the build step stops before pushing -----------------------------------------------


def test_approving_the_build_commits_locally_but_does_not_push(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = _bare_repo(tmp_path)
    settings = _configured(settings, bare)
    _patch_real_builder(settings, bare, monkeypatch)
    approval_id = asyncio.run(_paused_github_build(settings))

    service.resolve_approval(approval_id, True, settings=settings)

    # A bare repo with no commits ever pushed to it has no HEAD at all -- `git log`
    # errors (exit 128) rather than returning blank, which is itself the confirmation
    # nothing reached the remote, not a test failure to work around.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=bare, capture_output=True, text=True, check=False,
    )
    assert log.returncode != 0, "nothing should have reached the remote yet"


def test_approving_the_build_creates_a_pending_push_approval(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = _bare_repo(tmp_path)
    settings = _configured(settings, bare)
    _patch_real_builder(settings, bare, monkeypatch)
    approval_id = asyncio.run(_paused_github_build(settings))

    service.resolve_approval(approval_id, True, settings=settings)

    with repo.open_db(settings) as conn:
        pending = repo.list_pending_approvals(conn)
    push_rows = [a for a in pending if a.kind == "push"]
    assert len(push_rows) == 1
    assert push_rows[0].project_dir is not None
    assert Path(push_rows[0].project_dir).exists(), "the human needs somewhere real to look"


def test_the_free_mock_builder_still_pushes_immediately_unaffected(
    settings: Settings, tmp_path: Path,
) -> None:
    """Step 22 only changes behaviour for AXON_BUILDER=claude. The free default must
    push in one step exactly as it always has -- there is nothing worth a second look
    at in the placeholder README/stub."""
    bare = _bare_repo(tmp_path)
    settings = replace(settings, github_username="octocat")  # axon_builder stays "mock"
    approval_id = asyncio.run(_paused_github_build(settings))

    import axon.hands.github as github_module
    original_push_url = github_module.GitHubHand._push_url
    github_module.GitHubHand._push_url = lambda self, note: str(bare)
    try:
        service.resolve_approval(approval_id, True, settings=settings)
    finally:
        github_module.GitHubHand._push_url = original_push_url

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=bare, capture_output=True, text=True, check=True,
    )
    assert "Axon:" in log.stdout, "the mock path should have pushed in one step, as before"

    with repo.open_db(settings) as conn:
        pending = repo.list_pending_approvals(conn)
    assert not any(a.kind == "push" for a in pending), "no second gate for the free path"


# --- resolving the push approval ---------------------------------------------------------


def test_approving_the_push_approval_actually_pushes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = _bare_repo(tmp_path)
    settings = _configured(settings, bare)
    _patch_real_builder(settings, bare, monkeypatch)
    build_approval_id = asyncio.run(_paused_github_build(settings))
    service.resolve_approval(build_approval_id, True, settings=settings)

    with repo.open_db(settings) as conn:
        push_approval = next(a for a in repo.list_pending_approvals(conn) if a.kind == "push")

    service.resolve_push_approval(push_approval.id, True, settings=settings)

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=bare, capture_output=True, text=True, check=True,
    )
    assert "Axon (Claude Code):" in log.stdout


def test_rejecting_the_push_approval_never_pushes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of this feature: reject here and the build stays local
    forever -- nothing pushes, nothing gets deleted."""
    bare = _bare_repo(tmp_path)
    settings = _configured(settings, bare)
    _patch_real_builder(settings, bare, monkeypatch)
    build_approval_id = asyncio.run(_paused_github_build(settings))
    service.resolve_approval(build_approval_id, True, settings=settings)

    with repo.open_db(settings) as conn:
        push_approval = next(a for a in repo.list_pending_approvals(conn) if a.kind == "push")

    service.resolve_push_approval(push_approval.id, False, settings=settings)

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=bare, capture_output=True, text=True, check=False,
    )
    assert log.returncode != 0, "still nothing on the remote"
    assert Path(push_approval.project_dir).exists(), "the code is still sitting there, untouched"

    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, push_approval.id).status == "rejected"


def test_approving_a_push_approval_twice_is_rejected(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = _bare_repo(tmp_path)
    settings = _configured(settings, bare)
    _patch_real_builder(settings, bare, monkeypatch)
    build_approval_id = asyncio.run(_paused_github_build(settings))
    service.resolve_approval(build_approval_id, True, settings=settings)

    with repo.open_db(settings) as conn:
        push_approval = next(a for a in repo.list_pending_approvals(conn) if a.kind == "push")
    service.resolve_push_approval(push_approval.id, True, settings=settings)

    with pytest.raises(service.ApprovalAlreadyResolved):
        service.resolve_push_approval(push_approval.id, True, settings=settings)


def test_a_missing_remote_leaves_the_push_approval_pending_for_retry(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract every other ApprovalExecutionFailed path already gives: a failed
    push (e.g. the GitHub repo was never actually created) must not lose the build."""
    settings = replace(settings, github_username="octocat", axon_builder="claude")
    _patch_real_builder(settings, Path("does-not-matter"), monkeypatch)
    # Deliberately point at a URL nothing is listening on, instead of a bare repo.
    import axon.hands.github as github_module

    monkeypatch.setattr(
        github_module.GitHubHand, "_push_url",
        lambda self, note: str(tmp_path / "nonexistent" / "remote.git"),
    )
    build_approval_id = asyncio.run(_paused_github_build(settings))
    service.resolve_approval(build_approval_id, True, settings=settings)

    with repo.open_db(settings) as conn:
        push_approval = next(a for a in repo.list_pending_approvals(conn) if a.kind == "push")

    with pytest.raises(service.ApprovalExecutionFailed):
        service.resolve_push_approval(push_approval.id, True, settings=settings)

    with repo.open_db(settings) as conn:
        assert repo.get_approval(conn, push_approval.id).status == "pending"
