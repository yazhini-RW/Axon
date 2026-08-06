"""What actually writes the files for a GitHub note. See docs/V2-PLAN.md Step 8.

Same "same interface, mock stays default" pattern as axon.brain.classifier.Brain
(Step 6): a real Claude Code builder can slot in later (Step 12, paid) without the
GitHubHand or the workflow changing at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from axon.models import ClassifiedNote


class Builder(Protocol):
    """Writes real files into `project_dir`. Never touches git or the network."""

    name: str

    def build(self, note: ClassifiedNote, project_dir: Path) -> list[str]:
        """Write files under `project_dir` (already created). Returns filenames written."""
        ...


def get_builder(settings=None) -> "Builder":
    """Pick a builder: the real Claude Code one only if AXON_BUILDER=claude is set,
    the free mock otherwise. Same "same interface, mock stays default" pattern as
    axon.brain.classifier.get_brain — see docs/V2-PLAN.md Step 12.

    Deferred imports so a machine that never opts in never needs anything Step-12-only.
    """
    from axon.config import get_settings
    from axon.hands.builders.mock import MockBuilder

    settings = settings or get_settings()
    if settings.axon_builder == "claude":
        from axon.hands.builders.claude_code import ClaudeCodeBuilder

        return ClaudeCodeBuilder()
    return MockBuilder()
