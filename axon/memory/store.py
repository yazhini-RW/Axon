"""Long-term memory: remember a note, and find it later by meaning rather than words.

All local, no API key. See docs/adr/0002-free-local-only-stack.md.

Two constraints shape this module:

1. Local Qdrant is exclusive to a single process. Opening it twice raises "Storage
   folder is already accessed by another instance". So memory is opened for the length
   of one command and closed again — never held. The `axon run` daemon must not touch it.
2. Mem0 builds an LLM client eagerly in its constructor, even when it is never used.
   The placeholder key that works around that lives here and nowhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from axon.config import Settings, get_settings

if TYPE_CHECKING:
    from mem0 import Memory

# Mem0 ships posthog telemetry. Local means local — and this has to be set before mem0
# is imported, not after.
os.environ.setdefault("MEM0_TELEMETRY", "False")


def _import_mem0():
    """Imported late on purpose.

    mem0 + fastembed + onnxruntime cost about 3 seconds to import. Paying that in
    `axon list` or `axon doctor`, which never touch memory, would make the whole CLI
    feel broken. Only the commands that actually remember or recall wait for it.
    """
    import logging

    # Mem0 warns on every call that spaCy is missing. It only needs spaCy for a
    # lemmatiser Axon never uses, and the message looks like a real failure to the
    # user. Silence exactly that one logger, nothing broader.
    logging.getLogger("mem0.utils.spacy_models").setLevel(logging.ERROR)

    from mem0 import Memory

    return Memory

MEMORY_USER = "axon"
COLLECTION = "axon_memory"

# V1 never asks Mem0 to run LLM fact-extraction, which is what keeps it key-free.
# Do not flip this on without a real key present — see ADR-0002.
_USE_LLM_EXTRACTION = False

# Deliberately not a secret: a stand-in for a client that is built and never called.
_UNUSED_LLM_PLACEHOLDER = "unused-axon-offline-mode"  # NOSONAR


class MemoryLocked(RuntimeError):
    """Another axon command is already using memory."""


@dataclass
class Recollection:
    """One thing Axon remembered, and how confident it is."""

    text: str
    score: float
    note_id: int | None = None
    kind: str | None = None


def embedding_dims(model: str) -> int:
    """Ask fastembed how wide this model is. Metadata only — nothing is downloaded."""
    from fastembed import TextEmbedding

    supported = TextEmbedding.list_supported_models()
    for candidate in supported:
        if candidate["model"].lower() == model.lower():
            return int(candidate["dim"])
    names = ", ".join(sorted(m["model"] for m in supported)[:5])
    raise ValueError(f"unknown embedding model {model!r}. Try one of: {names}, ...")


class MemoryStore:
    """Use as a context manager so the storage lock is always released."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._memory: "Memory | None" = None

    def _config(self) -> dict[str, Any]:
        model = self._settings.embed_model
        return {
            # Not a credential. Mem0 insists on building an LLM client in its
            # constructor, but every write below passes infer=False so it is never
            # invoked and never authenticates anything. See ADR-0002.
            "llm": {"provider": "openai", "config": {"api_key": _UNUSED_LLM_PLACEHOLDER}},
            "embedder": {"provider": "fastembed", "config": {"model": model}},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": str(self._settings.vector_dir),
                    "collection_name": COLLECTION,
                    "embedding_model_dims": embedding_dims(model),
                    "on_disk": True,
                },
            },
        }

    def open(self) -> MemoryStore:
        """Load the model and take the storage lock. Downloads the model on first run."""
        if self._memory is not None:
            return self
        self._settings.ensure_dirs()
        memory_cls = _import_mem0()
        try:
            self._memory = memory_cls.from_config(self._config())
        except RuntimeError as exc:
            if "already accessed" in str(exc):
                raise MemoryLocked(
                    "memory is in use by another axon command — try again in a moment"
                ) from exc
            raise
        return self

    def close(self) -> None:
        """Release the storage lock. Without this, the next command is blocked."""
        if self._memory is None:
            return
        try:
            self._memory.vector_store.client.close()
        except Exception:  # noqa: BLE001 - closing must never be the thing that fails
            pass
        finally:
            self._memory = None

    def __enter__(self) -> MemoryStore:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    def _require(self) -> "Memory":
        if self._memory is None:
            raise RuntimeError("memory is not open — use `with MemoryStore() as memory:`")
        return self._memory

    def remember(self, text: str, note_id: int | None = None, kind: str | None = None) -> None:
        """Store a note so it can be found later by meaning."""
        metadata: dict[str, Any] = {}
        if note_id is not None:
            metadata["note_id"] = note_id
        if kind is not None:
            metadata["kind"] = kind

        self._require().add(
            text,
            user_id=MEMORY_USER,
            metadata=metadata or None,
            infer=_USE_LLM_EXTRACTION,
        )

    def recall(self, query: str, limit: int = 5) -> list[Recollection]:
        """Find notes that *mean* something like the query.

        Returns a ranked list, not a single answer: embedding search can be confidently
        wrong, so the caller shows scores and lets the human judge. See SPEC known limits.
        """
        # Mem0 2.x rejects user_id as a top-level argument here, unlike add().
        raw = self._require().search(query, filters={"user_id": MEMORY_USER}, limit=limit)
        results = raw.get("results", []) if isinstance(raw, dict) else (raw or [])

        recollections = []
        for hit in results:
            meta = hit.get("metadata") or {}
            recollections.append(
                Recollection(
                    text=hit.get("memory", ""),
                    score=float(hit.get("score", 0.0)),
                    note_id=meta.get("note_id"),
                    kind=meta.get("kind"),
                )
            )
        return recollections

    def count(self) -> int:
        raw = self._require().get_all(filters={"user_id": MEMORY_USER})
        results = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
        return len(results)
