# ADR-0002: Everything free, everything local, mock-first

- **Status:** Accepted
- **Date:** 2026-07-30

## Context

Axon is a personal side project. It must cost nothing to run and must work with no API
key at all. "Works if you have a key" is not acceptable — the no-key path is the
*default* path, not a degraded fallback.

## Decision

### The thinking layer: mock first, Gemini optional

A rule-based mock classifier is the default. If `GEMINI_API_KEY` is set, Axon uses
Google Gemini's free tier instead. Same interface, swapped implementation.

**There is no Gemini connector package for Agent Framework.** `agent-framework-google`
and `agent-framework-gemini` do not exist on PyPI (checked). Gemini publishes an
**OpenAI-compatible endpoint**, so Axon will use `agent-framework-openai` pointed at
Gemini's base URL. This is Step 6 and is entirely optional.

### Memory: Mem0 + Qdrant + fastembed, no key

| Piece | Choice | Why |
|---|---|---|
| Memory layer | `mem0ai` | as chosen for the project |
| Vector store | `qdrant-client` local on-disk mode | no server to run, no key |
| Embeddings | `fastembed`, `BAAI/bge-small-en-v1.5` | local ONNX, no key |

**Model choice:** Mem0's fastembed default is `thenlper/gte-large` (**1.2GB**, 1024 dims).
Axon defaults to `BAAI/bge-small-en-v1.5` (134MB on disk, 384 dims) — nearly ten times
lighter, for comparable quality on short notes. Overridable via `AXON_EMBED_MODEL`.

Dimensions are looked up at runtime with `TextEmbedding.list_supported_models()`, which
reads metadata only and downloads nothing, so overriding the model cannot silently
mismatch the vector store.

### The placeholder-key workaround

`Memory.__init__` constructs an LLM client **eagerly**, defaulting to OpenAI, and raises
`OpenAIError: Missing credentials` without a key — *even if the LLM is never used*.

Axon therefore, in mock mode:
- passes `llm.config.api_key = "unused-axon-offline-mode"`, and
- only ever calls `memory.add(..., infer=False)`, which skips Mem0's LLM-based fact
  extraction entirely.

The LLM object is built and never invoked. **`infer=True` must be guarded** so it is only
reachable when a real key is configured — otherwise it would fail confusingly at runtime.

### Telemetry off

Mem0 pulls in `posthog`. Axon sets `MEM0_TELEMETRY=False` before importing it. Local
means local.

## Consequences

- Verified: 4 notes stored and semantically searched with **no API key present**.
  `"groceries"` correctly retrieved `"buy milk at 5pm"` (0.658) with no shared word.
- One-time ~130MB model download on first run. After that, fully offline.
- The placeholder key is a real wart. It is documented here rather than hidden, and is
  contained to one function in `axon/memory/`.
- Mem0 2.x API asymmetry to remember: `add()` takes `user_id=` at top level, but
  `search()` requires `filters={"user_id": ...}` and rejects the top-level form.
- **Local Qdrant is exclusive to one process.** A second client on the same folder raises
  `"Storage folder ... is already accessed by another instance"`. Two consequences, both
  binding:
  1. Memory is opened for the duration of one command and closed again, never held open.
     `MemoryStore` is a context manager so the lock is always released.
  2. **The `axon run` scheduler daemon must never touch memory**, or every `axon add`
     and `axon recall` would fail while it is running. The daemon uses SQLite only.
- **Importing mem0 costs ~3 seconds** (it pulls in onnxruntime), and loading the model
  another ~2.4s. Those imports are therefore deferred into the functions that need them,
  so `axon list` and `axon doctor` stay instant. Do not hoist them to module level.
- Mem0 logs a spaCy warning on every call for a lemmatiser Axon never uses. Exactly that
  one logger is silenced; nothing broader, so real errors still surface.
- If Mem0 becomes more trouble than it is worth, the fallback is Qdrant + fastembed
  directly behind the same `axon/memory/store.py` interface — no other module changes.
