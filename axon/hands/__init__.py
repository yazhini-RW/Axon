"""Hands: what Axon can actually do about a note, split into safe and risky halves.

See docs/V2-PLAN.md Step 7. Every hand splits into `prepare` (safe, runs immediately)
and `execute` (risky, only runs after the human approval gate says yes).
"""

from axon.hands.base import Hand
from axon.hands.chat import ChatHand, looks_like_a_chat_note
from axon.hands.email import EmailHand, looks_like_an_email_note
from axon.hands.files import FilesHand, looks_like_a_files_note
from axon.hands.github import GitHubHand, looks_like_a_github_note
from axon.hands.noop import NoopHand

__all__ = [
    "Hand", "NoopHand", "GitHubHand", "looks_like_a_github_note",
    "EmailHand", "looks_like_an_email_note",
    "ChatHand", "looks_like_a_chat_note",
    "FilesHand", "looks_like_a_files_note",
]


def pick_hand(text: str, settings=None) -> Hand:
    """Which hand should act on this note? NoopHand unless it looks shaped for one of
    the real hands. Order matters only in the (rare) case a note could match more than
    one — GitHub/email/chat/files notes essentially never overlap in practice, so this
    is mostly first-match-wins for clarity, not a real precedence conflict."""
    if looks_like_a_github_note(text):
        return GitHubHand(settings=settings)
    if looks_like_an_email_note(text):
        return EmailHand(settings=settings)
    if looks_like_a_chat_note(text):
        return ChatHand(settings=settings)
    if looks_like_a_files_note(text):
        return FilesHand(settings=settings)
    return NoopHand()
