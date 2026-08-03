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
    github_username: str | None = None
    projects_dir: Path = Path("./projects")
    # V3 Step 13: email hand. Free via a Gmail "app password" (not your real password —
    # myaccount.google.com/apppasswords), SMTP+STARTTLS, no paid service.
    email_address: str | None = None
    email_app_password: str | None = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    # V3 Step 14: chat hand. A Slack or (classic) Teams incoming webhook URL — both
    # accept a simple {"text": ...} JSON body, so one hand covers either.
    chat_webhook_url: str | None = None
    # V3 Step 15: files & docs hand. Real deliverables, kept separate from
    # projects_dir (GitHub-built code) and data/ (Axon's own internal state).
    documents_dir: Path = Path("./documents")

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
    username = (os.getenv("GITHUB_USERNAME") or "").strip()
    email_address = (os.getenv("EMAIL_ADDRESS") or "").strip()
    email_app_password = (os.getenv("EMAIL_APP_PASSWORD") or "").strip()
    chat_webhook_url = (os.getenv("CHAT_WEBHOOK_URL") or "").strip()
    return Settings(
        data_dir=Path(os.getenv("AXON_DATA_DIR") or "./data").resolve(),
        embed_model=os.getenv("AXON_EMBED_MODEL") or DEFAULT_EMBED_MODEL,
        gemini_api_key=key or None,
        github_username=username or None,
        projects_dir=Path(os.getenv("AXON_PROJECTS_DIR") or "./projects").resolve(),
        email_address=email_address or None,
        email_app_password=email_app_password or None,
        smtp_host=os.getenv("SMTP_HOST") or "smtp.gmail.com",
        smtp_port=int(os.getenv("SMTP_PORT") or "587"),
        chat_webhook_url=chat_webhook_url or None,
        documents_dir=Path(os.getenv("AXON_DOCUMENTS_DIR") or "./documents").resolve(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
