"""Real memory, no mocking of the vector store.

These load a 134MB embedding model, so they are slower than the rest. Run just the fast
suite with: pytest -m "not slow"
"""

from __future__ import annotations

import pytest

from axon.config import Settings
from axon.memory.store import MemoryLocked, MemoryStore, embedding_dims

pytestmark = pytest.mark.slow


def test_embedding_dims_needs_no_download() -> None:
    assert embedding_dims("BAAI/bge-small-en-v1.5") == 384
    assert embedding_dims("thenlper/gte-large") == 1024


def test_unknown_model_fails_clearly() -> None:
    with pytest.raises(ValueError, match="unknown embedding model"):
        embedding_dims("not-a-real-model")


def test_remember_then_recall_by_meaning(settings: Settings) -> None:
    """The whole point of Axon: find a note using words it does not contain."""
    with MemoryStore(settings) as memory:
        memory.remember("buy milk at 5pm", note_id=1, kind="reminder")
        memory.remember("the office wifi password is on the whiteboard", note_id=2, kind="fact")

        hits = memory.recall("groceries", limit=3)

    assert hits, "expected some recollection"
    assert hits[0].text == "buy milk at 5pm"
    assert "groceries" not in hits[0].text, "matched by meaning, not by word"


def test_recall_returns_results_ranked_by_score(settings: Settings) -> None:
    with MemoryStore(settings) as memory:
        memory.remember("buy milk at 5pm", note_id=1)
        memory.remember("we chose Microsoft Agent Framework", note_id=2)
        memory.remember("the office wifi password is on the whiteboard", note_id=3)

        hits = memory.recall("groceries", limit=3)

    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True), "must come back best-first"


def test_metadata_survives_the_round_trip(settings: Settings) -> None:
    with MemoryStore(settings) as memory:
        memory.remember("buy milk at 5pm", note_id=42, kind="reminder")
        top = memory.recall("groceries", limit=1)[0]

    assert top.note_id == 42
    assert top.kind == "reminder"


def test_memory_survives_being_closed_and_reopened(settings: Settings) -> None:
    """Long-term means it outlives the process."""
    with MemoryStore(settings) as memory:
        memory.remember("we chose Microsoft Agent Framework", note_id=1, kind="fact")

    with MemoryStore(settings) as reopened:
        hits = reopened.recall("which agent library did I pick", limit=1)

    assert hits and "Agent Framework" in hits[0].text


def test_recall_on_empty_memory_is_not_an_error(settings: Settings) -> None:
    with MemoryStore(settings) as memory:
        assert memory.recall("anything at all") == []


def test_two_stores_at_once_give_a_clear_error(settings: Settings) -> None:
    """Local Qdrant is single-process. The message must say so in plain English."""
    with MemoryStore(settings):
        with pytest.raises(MemoryLocked, match="another axon command"):
            MemoryStore(settings).open()


def test_closing_releases_the_lock(settings: Settings) -> None:
    """If close() leaked the lock, every later command would fail."""
    first = MemoryStore(settings).open()
    first.close()

    second = MemoryStore(settings).open()  # would raise if the lock leaked
    second.close()


def test_using_a_closed_store_is_a_clear_error(settings: Settings) -> None:
    store = MemoryStore(settings)
    with pytest.raises(RuntimeError, match="not open"):
        store.recall("anything")
