"""The FastAPI backend (Step 10) — same operations as the CLI, over HTTP.

axon.web.api is a thin JSON layer over axon.service; these tests exist to prove that
layer (status codes, response shapes, error mapping), not to re-test axon.service's
own logic — that's already covered by tests/test_workflow.py and tests/test_github_hand.py.

Unlike the rest of the suite, axon.service and axon.web.api read settings via
get_settings() by default (there's no per-request way to inject a Settings object into
a FastAPI route the way tests elsewhere pass settings= directly). So this points
AXON_DATA_DIR / AXON_PROJECTS_DIR at tmp_path via env vars and clears the lru_cache,
the same way a real deployment would configure them via .env.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from axon.config import get_settings


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AXON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AXON_PROJECTS_DIR", str(tmp_path / "projects"))
    # Added after a real leak: a files-hand test through this client wrote into the
    # real project's ./documents (Step 16) because this fixture predates
    # documents_dir (Step 15) and was never updated — the same class of mistake
    # ADR-0002 warns about, just in a different fixture this time.
    monkeypatch.setenv("AXON_DOCUMENTS_DIR", str(tmp_path / "documents"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_USERNAME", raising=False)
    get_settings.cache_clear()

    from axon.web.api import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()


# --- notes -----------------------------------------------------------------------


def test_add_a_non_risky_note_completes_immediately(client: TestClient) -> None:
    resp = client.post("/api/notes", json={"text": "fix the login bug"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["kind"] == "task"
    assert body["note"]["text"] == "fix the login bug"
    assert body["note"]["id"] == 1


def test_a_completed_non_risky_note_still_shows_its_hand_detail(client: TestClient) -> None:
    """Regression (Step 16): a hand's prepare()-time detail (e.g. the files hand's
    "wrote <path>", or the research hand's actual answer) was silently dropped for
    every non-risky note, since the completed path never carried it. Uses the files
    hand here since it needs no network mocking - same underlying bug either way."""
    resp = client.post("/api/notes", json={"text": "save my grocery list to groceries.txt"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["detail"]
    assert "groceries.txt" in body["detail"]


def test_add_an_empty_note_is_a_400(client: TestClient) -> None:
    resp = client.post("/api/notes", json={"text": "   "})
    assert resp.status_code == 400


def test_add_a_risky_note_pauses_and_returns_the_approval_id(client: TestClient) -> None:
    resp = client.post("/api/notes", json={"text": "send the invoice to the client"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["action"] == "send"
    assert body["approval_id"] is not None


def test_list_notes_reflects_what_was_added(client: TestClient) -> None:
    client.post("/api/notes", json={"text": "fix the login bug"})
    client.post("/api/notes", json={"text": "buy milk at 5pm"})

    resp = client.get("/api/notes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["notes"]) == 2


def test_list_notes_respects_limit(client: TestClient) -> None:
    for i in range(3):
        client.post("/api/notes", json={"text": f"task number {i}"})

    resp = client.get("/api/notes", params={"limit": 1})
    assert resp.status_code == 200
    assert len(resp.json()["notes"]) == 1
    assert resp.json()["total"] == 3


# --- approvals ---------------------------------------------------------------------


def test_pending_note_shows_up_in_approvals(client: TestClient) -> None:
    add = client.post("/api/notes", json={"text": "send the invoice to the client"})
    approval_id = add.json()["approval_id"]

    resp = client.get("/api/approvals")
    assert resp.status_code == 200
    approvals = resp.json()["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["id"] == approval_id
    assert approvals[0]["action"] == "send"


def test_approving_resolves_and_removes_it_from_the_pending_list(client: TestClient) -> None:
    add = client.post("/api/notes", json={"text": "send the invoice to the client"})
    approval_id = add.json()["approval_id"]

    resp = client.post(f"/api/approvals/{approval_id}/approve")
    assert resp.status_code == 200
    assert resp.json()["approved"] is True

    still_pending = client.get("/api/approvals").json()["approvals"]
    assert still_pending == []


def test_rejecting_resolves_it_too(client: TestClient) -> None:
    add = client.post("/api/notes", json={"text": "send the invoice to the client"})
    approval_id = add.json()["approval_id"]

    resp = client.post(f"/api/approvals/{approval_id}/reject")
    assert resp.status_code == 200
    assert resp.json()["approved"] is False


def test_approving_twice_is_a_409(client: TestClient) -> None:
    add = client.post("/api/notes", json={"text": "send the invoice to the client"})
    approval_id = add.json()["approval_id"]

    client.post(f"/api/approvals/{approval_id}/approve")
    resp = client.post(f"/api/approvals/{approval_id}/approve")
    assert resp.status_code == 409


def test_approving_an_unknown_id_is_a_404(client: TestClient) -> None:
    resp = client.post("/api/approvals/999/approve")
    assert resp.status_code == 404


# --- github hand end to end over http -----------------------------------------------


def test_a_github_push_note_shows_the_exact_command_before_approval(client: TestClient) -> None:
    """The plan's "no surprises" requirement, now proven over HTTP too — this note has
    no GITHUB_USERNAME configured, so push_url is None and the UI (Step 11) would show
    no command, same as the CLI would."""
    resp = client.post("/api/notes", json={"text": "push the repo to github"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert "committed" in body["detail"]
    assert body["push_url"] is None  # no GITHUB_USERNAME in this test's env


def test_approving_a_push_with_no_github_username_is_a_clean_502_not_a_crash(
    client: TestClient,
) -> None:
    """Regression: found by manually running `axon serve` and approving a real GitHub
    note with no GITHUB_USERNAME configured — GitHubHand.execute() raised and the
    exception reached FastAPI unhandled, returning a raw 500 with a traceback instead
    of a clean error. The approval must also stay retryable, not get marked resolved."""
    add = client.post("/api/notes", json={"text": "push the repo to github"})
    approval_id = add.json()["approval_id"]

    resp = client.post(f"/api/approvals/{approval_id}/approve")

    assert resp.status_code == 502
    assert "GITHUB_USERNAME" in resp.json()["detail"]

    still_pending = client.get("/api/approvals").json()["approvals"]
    assert len(still_pending) == 1, "a failed push must leave the approval pending, not resolved"


# --- doctor --------------------------------------------------------------------------


def test_doctor_reports_the_mock_brain_and_no_paid_services(client: TestClient) -> None:
    resp = client.get("/api/doctor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["brain_mode"] == "mock"
    assert body["schema_version"] >= 3


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- concurrency (Step 11 UI fires several requests at once) --------------------------


def test_concurrent_reads_dont_hit_database_is_locked(client: TestClient) -> None:
    """Regression: found by actually driving the Step 11 UI with a real browser, not
    by this test suite. The page fires GET /api/notes, /api/approvals and /api/doctor
    together after every add (Promise.all in index.html). FastAPI runs sync routes in a
    threadpool, so these can genuinely overlap with a write still landing (the add
    itself, or a background WAL checkpoint) — and SQLite's default is to fail
    immediately with "database is locked" rather than wait. Fixed with
    PRAGMA busy_timeout in axon/db/repo.py's connect().

    Not covered by this test: concurrent *writes* to notes. Two overlapping
    `POST /api/notes` calls both try to open memory (Qdrant) at once and correctly get
    423 MemoryLocked from the other one - that's ADR-0002's single-process constraint
    working as intended, not a bug, and not something the UI's add form can even
    trigger (its button disables itself while a request is in flight)."""
    import concurrent.futures

    client.post("/api/notes", json={"text": "fix the login bug"})

    def read(path: str) -> int:
        return client.get(path).status_code

    paths = ["/api/notes", "/api/approvals", "/api/doctor"] * 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as pool:
        statuses = list(pool.map(read, paths))

    assert all(s == 200 for s in statuses), statuses


def test_concurrent_first_requests_dont_race_the_schema_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: several concurrent requests against a brand-new database (schema
    v0) can all read `version < 3` before any of them finishes migrating, and then all
    try `ALTER TABLE approvals ADD COLUMN detail` — only the first succeeds, the rest
    raised sqlite3.OperationalError: duplicate column name. Fixed in repo.init_db by
    treating that specific error as "another connection already did this migration"."""
    import concurrent.futures

    monkeypatch.setenv("AXON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AXON_PROJECTS_DIR", str(tmp_path / "projects"))
    get_settings.cache_clear()

    from axon.web.api import app

    with TestClient(app) as fresh_client:

        def hit(_: int) -> int:
            return fresh_client.get("/api/notes").status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(hit, range(12)))

    get_settings.cache_clear()
    assert all(s == 200 for s in statuses), statuses


# --- recall (loads the embedding model — slow) ----------------------------------------


@pytest.mark.slow
def test_recall_finds_a_note_by_meaning(client: TestClient) -> None:
    client.post("/api/notes", json={"text": "the wifi password is on the whiteboard"})

    resp = client.get("/api/recall", params={"q": "how do I connect to wifi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["shown"], "expected at least one related memory"
    assert "wifi" in body["shown"][0]["text"].lower()
