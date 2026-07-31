"""Hands: what Axon can actually do about a note, split into safe and risky halves.

See docs/V2-PLAN.md Step 7. Every hand splits into `prepare` (safe, runs immediately)
and `execute` (risky, only runs after the human approval gate says yes).
"""

from axon.hands.base import Hand
from axon.hands.noop import NoopHand

__all__ = ["Hand", "NoopHand"]
