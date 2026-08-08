"""Where Axon keeps things, and which brain it will use.

Everything here has a working default, so Axon runs with no .env file at all.
"""

from __future__ import annotations

import os
import re
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
    # V3 Step 17: WhatsApp via Meta's Cloud API. Free on the test-number tier -- a test
    # sender Meta provides, messaging only numbers you verified in their dashboard.
    # A business-initiated WhatsApp message cannot be arbitrary text: it must go through
    # an approved template, so the template name is configurable rather than hardcoded
    # (which template exists depends on the account, and Axon shouldn't guess).
    whatsapp_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_to: str | None = None
    whatsapp_template: str = "hello_world"
    # Step 12: real Claude Code builder for GitHub projects. Opt-in and off by default —
    # unlike every other setting here, this one spends real Claude usage (your Pro
    # plan's shared pool) every time an approved GitHub-build note runs. The free
    # MockBuilder is used unless this is explicitly set to "claude".
    axon_builder: str = "mock"

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
    # Google *displays* an app password as "abcd efgh ijkl mnop", so pasting it exactly
    # as shown is the natural thing to do — but SMTP login needs the bare 16 characters,
    # and the separators Google renders are non-breaking spaces (\xa0), not plain ones.
    # Left unhandled that surfaces as a UnicodeEncodeError deep inside smtplib rather
    # than anything resembling "your password has spaces in it". Strip all whitespace.
    email_app_password = re.sub(r"\s", "", os.getenv("EMAIL_APP_PASSWORD") or "")
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
        whatsapp_token=(os.getenv("WHATSAPP_TOKEN") or "").strip() or None,
        whatsapp_phone_number_id=(os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip() or None,
        # Digits only: WhatsApp wants no '+', and a pasted number often carries spaces
        # or dashes from wherever it was copied from.
        whatsapp_to=re.sub(r"\D", "", os.getenv("WHATSAPP_TO") or "") or None,
        whatsapp_template=(os.getenv("WHATSAPP_TEMPLATE") or "hello_world").strip(),
        axon_builder=(os.getenv("AXON_BUILDER") or "mock").strip().lower(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
