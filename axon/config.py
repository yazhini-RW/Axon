"""Where Axon keeps things, and which brain it will use.

Everything here has a working default, so Axon runs with no .env file at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Small, fast, no API key. See docs/adr/0002-free-local-only-stack.md for why this
# rather than Mem0's much larger default.
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    embed_model: str
    gemini_api_key: str | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "axon.db"

    @property
    def vector_dir(self) -> Path:
        return self.data_dir / "qdrant"

    @property
    def checkpoint_dir(self) -> Path:
        return self.data_dir / "checkpoints"

    @property
    def brain_mode(self) -> str:
        """'gemini' when a key is present, otherwise the free rule-based brain."""
        return "gemini" if self.gemini_api_key else "mock"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    return Settings(
        data_dir=Path(os.getenv("AXON_DATA_DIR") or "./data").resolve(),
        embed_model=os.getenv("AXON_EMBED_MODEL") or DEFAULT_EMBED_MODEL,
        gemini_api_key=key or None,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
