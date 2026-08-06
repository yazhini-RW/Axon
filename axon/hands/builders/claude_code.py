"""The real builder: Step 12. Spawns Claude Code to actually write the project.

NOT used in GitHubHand.prepare() — that stays on MockBuilder so drafting a note never
costs anything. This only ever runs from GitHubHand.execute(), after a human has
approved, because it spends real Claude usage (your Pro plan's shared pool, not a
separate bill — see docs/V2-PLAN.md Step 12) every single time it runs.

Runs `claude -p <prompt>` as a one-shot, non-interactive subprocess with its cwd set to
the project folder Axon already created — verified safe to shell out to this way (see
Step 12's commit message: a `claude --version` call made *from inside* a running Claude
Code session behaved strangely, but a fresh process in its own terminal ran cleanly and
exited with real JSON). `--permission-mode acceptEdits` is what lets it actually write
files with no human watching to click "yes" on each one; scoped only by `cwd`, not
`--dangerously-skip-permissions`, since this only ever needs to touch project_dir.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from axon.models import ClassifiedNote


class ClaudeCodeBuildError(RuntimeError):
    """The claude CLI ran but didn't produce a usable result."""


class ClaudeCodeBuilder:
    name = "claude"

    def build(self, note: ClassifiedNote, project_dir: Path) -> list[str]:
        before = {p for p in project_dir.rglob("*") if p.is_file()}

        prompt = (
            f"Write a small, real, working project for this request: {note.text!r}\n"
            "Create whatever files are needed directly in the current directory. "
            "Keep it simple and self-contained — no placeholder/TODO code, no extra "
            "explanation text outside the files themselves."
        )
        result = subprocess.run(
            ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
             "--output-format", "json"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise ClaudeCodeBuildError(
                f"claude exited {result.returncode}: {result.stderr.strip()[:500]}"
            )

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeCodeBuildError(f"claude returned non-JSON output: {exc}") from exc
        if payload.get("is_error"):
            raise ClaudeCodeBuildError(f"claude reported an error: {payload.get('result')}")

        after = {p for p in project_dir.rglob("*") if p.is_file()}
        written = after - before
        if not written:
            raise ClaudeCodeBuildError(
                f"claude finished but wrote no files — it said: {payload.get('result', '')[:300]}"
            )
        return sorted(str(p.relative_to(project_dir)) for p in written)
