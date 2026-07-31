"""Hands: what Axon can actually do about a note, split into safe and risky halves.

See docs/V2-PLAN.md Step 7. Every hand splits into `prepare` (safe, runs immediately)
and `execute` (risky, only runs after the human approval gate says yes).
"""

from axon.hands.base import Hand
from axon.hands.github import GitHubHand, looks_like_a_github_note
from axon.hands.noop import NoopHand

__all__ = ["Hand", "NoopHand", "GitHubHand", "looks_like_a_github_note"]


def pick_hand(text: str, settings=None) -> Hand:
    """Which hand should act on this note? NoopHand unless it looks GitHub-shaped."""
    if looks_like_a_github_note(text):
        return GitHubHand(settings=settings)
    return NoopHand()
