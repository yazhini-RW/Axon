"""The Hand interface: every future capability (GitHub, email, shopping, ...) implements
this, splitting its work into a safe half and a risky half either side of the approval
gate. See docs/V2-PLAN.md Step 7.
"""

from __future__ import annotations

from typing import Protocol

from axon.models import ApprovalOutcome, ClassifiedNote, PreparedNote


class Hand(Protocol):
    """`prepare` always runs. `execute` only runs once a human has approved."""

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        """The safe half: build, draft, stage — nothing irreversible yet."""
        ...

    async def execute(self, outcome: ApprovalOutcome) -> None:
        """The risky half: push, send, pay. Only called when outcome.approved is True."""
        ...
