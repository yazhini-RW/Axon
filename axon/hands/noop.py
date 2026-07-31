"""The default hand: does nothing. Keeps the `prepare -> gate -> execute` graph shape
real without any note actually being acted on yet. See docs/V2-PLAN.md Step 7.
"""

from __future__ import annotations

from axon.models import ApprovalOutcome, ClassifiedNote, PreparedNote


class NoopHand:
    """Every note gets this hand until Step 8 adds a real one for GitHub notes."""

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        return PreparedNote(note=note)

    async def execute(self, outcome: ApprovalOutcome) -> None:
        return None
