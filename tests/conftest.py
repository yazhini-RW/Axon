from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from axon.config import DEFAULT_EMBED_MODEL, Settings
from axon.db import repo


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Point Axon at a throwaway data directory, never the real one."""
    return Settings(data_dir=tmp_path, embed_model=DEFAULT_EMBED_MODEL, gemini_api_key=None)


@pytest.fixture
def conn(settings: Settings) -> Iterator:
    with repo.open_db(settings) as connection:
        yield connection
