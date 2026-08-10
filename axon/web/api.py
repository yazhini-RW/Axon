"""The web backend (Step 10). Same operations as the CLI, over HTTP.

This is a second front door to axon.service, not a second implementation of anything —
see axon/cli.py's module docstring. Every route here does argument parsing + JSON
shaping only; all real logic lives in axon.service, so behaviour never drifts between
`axon add` and `POST /api/notes`.

Memory (Qdrant) is single-process (ADR-0002), so this opens and closes it once per
request, exactly like the CLI opens it once per command — never held across requests.
Run with `axon serve` (uvicorn under the hood). Step 11 mounts the static HTML UI here
too, so one process serves both the API and the page.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from axon import service
from axon.models import Note

app = FastAPI(
    title="Axon",
    description="A second brain that remembers your notes and acts on them.",
)

# Step 11: one plain HTML file, inline CSS/JS, no npm and no build step — it calls the
# same /api/* routes below via fetch(). Served directly rather than mounted with
# StaticFiles since there's exactly one file and no other assets.
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# --- response/request shapes ---------------------------------------------------------
# Plain pydantic models, not the internal dataclasses directly — the API is a contract
# Step 11's UI depends on; it should stay stable even if axon.service's own return
# shapes change.


class NoteOut(BaseModel):
    id: int
    text: str
    kind: str
    status: str
    due_at: datetime | None
    created_at: datetime | None

    @classmethod
    def from_note(cls, note: Note) -> "NoteOut":
        return cls(
            id=note.id, text=note.text, kind=note.kind.value, status=note.status.value,
            due_at=note.due_at, created_at=note.created_at,
        )


class AddNoteRequest(BaseModel):
    text: str


class AddNoteResponse(BaseModel):
    note: NoteOut
    status: Literal["completed", "pending"]
    kind: str | None = None
    reason: str | None = None
    due_at: datetime | None = None
    recurring: bool = False
    approval_id: int | None = None
    action: str | None = None
    detail: str | None = None
    push_url: str | None = None


class NotesListResponse(BaseModel):
    notes: list[NoteOut]
    total: int


class RecallHit(BaseModel):
    text: str
    score: float
    note_id: int | None
    kind: str | None


class RecallResponse(BaseModel):
    shown: list[RecallHit]
    unsure: bool
    nothing_related: bool


class ApprovalOut(BaseModel):
    id: int
    note_id: int
    action: str
    note_text: str
    detail: str
    project_dir: str | None
    push_url: str | None
    # Step 18: the message itself, for the hands that send one. The UI shows this
    # verbatim so the human approves what will actually be delivered, not a summary.
    draft_subject: str | None = None
    draft_body: str | None = None


class ApprovalsListResponse(BaseModel):
    approvals: list[ApprovalOut]


class ResolveResponse(BaseModel):
    approval_id: int
    approved: bool
    action: str
    note_text: str
    paused_again: bool


class DoctorResponse(BaseModel):
    brain_mode: str
    embed_model: str
    data_dir: str
    db_path: str
    schema_version: int
    notes_total: int
    approvals_pending: int
    memory_ready: bool
    vector_dir: str


# --- routes ----------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/notes", response_model=AddNoteResponse)
def add_note(body: AddNoteRequest) -> AddNoteResponse:
    try:
        result = service.add_note(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.MemoryLocked as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except Exception as exc:  # the note is already saved; report but don't 500 it away
        raise HTTPException(
            status_code=502,
            detail=f"saved as unclassified — couldn't classify it just now: {exc}",
        ) from exc

    if result.pending_approval_id is not None:
        return AddNoteResponse(
            note=NoteOut.from_note(result.note),
            status="pending",
            approval_id=result.pending_approval_id,
            action=result.pending_action,
            detail=result.pending_detail,
            push_url=result.pending_push_url,
        )

    verdict = result.completed.note.classification
    return AddNoteResponse(
        note=NoteOut.from_note(result.note),
        status="completed",
        kind=verdict.kind.value,
        reason=verdict.reason,
        due_at=verdict.due_at,
        recurring=verdict.recurring,
        detail=result.completed.detail or None,
    )


@app.get("/api/notes", response_model=NotesListResponse)
def list_notes(limit: int = 20) -> NotesListResponse:
    notes, total = service.list_notes(limit=limit)
    return NotesListResponse(notes=[NoteOut.from_note(n) for n in notes], total=total)


@app.get("/api/recall", response_model=RecallResponse)
def recall(q: str, limit: int = 5) -> RecallResponse:
    try:
        result = service.recall(q, limit=limit)
    except service.MemoryLocked as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc

    return RecallResponse(
        shown=[
            RecallHit(text=h.text, score=h.score, note_id=h.note_id, kind=h.kind)
            for h in result.shown
        ],
        unsure=result.unsure,
        nothing_related=bool(result.hits) and not result.shown,
    )


@app.get("/api/approvals", response_model=ApprovalsListResponse)
def list_approvals() -> ApprovalsListResponse:
    pending = service.list_approvals()
    return ApprovalsListResponse(
        approvals=[
            ApprovalOut(
                id=a.id, note_id=a.note_id, action=a.action, note_text=a.note_text,
                detail=a.detail, project_dir=a.project_dir, push_url=a.push_url,
                draft_subject=a.draft_subject, draft_body=a.draft_body,
            )
            for a in pending
        ]
    )


def _resolve(approval_id: int, approved: bool) -> ResolveResponse:
    try:
        result = service.resolve_approval(approval_id, approved)
    except service.ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no approval #{approval_id}") from exc
    except service.ApprovalAlreadyResolved as exc:
        raise HTTPException(
            status_code=409, detail=f"approval #{approval_id} was already {exc.status}"
        ) from exc
    except service.ApprovalExecutionFailed as exc:
        # The approval is left `pending` by resolve_approval — the local commit is
        # safe, so this is retryable, not a fatal error. 502: the failure happened one
        # hop downstream (git / the hand), not in this request itself.
        raise HTTPException(
            status_code=502,
            detail=f"{exc} (approval #{approval_id} is still pending — fix and retry)",
        ) from exc

    return ResolveResponse(
        approval_id=result.approval_id, approved=result.approved, action=result.action,
        note_text=result.note_text, paused_again=result.paused_again,
    )


@app.post("/api/approvals/{approval_id}/approve", response_model=ResolveResponse)
def approve(approval_id: int) -> ResolveResponse:
    return _resolve(approval_id, approved=True)


@app.post("/api/approvals/{approval_id}/reject", response_model=ResolveResponse)
def reject(approval_id: int) -> ResolveResponse:
    return _resolve(approval_id, approved=False)


@app.get("/api/doctor", response_model=DoctorResponse)
def doctor() -> DoctorResponse:
    info = service.doctor_info()
    return DoctorResponse(
        brain_mode=info.brain_mode, embed_model=info.embed_model, data_dir=info.data_dir,
        db_path=info.db_path, schema_version=info.schema_version, notes_total=info.notes_total,
        approvals_pending=info.approvals_pending, memory_ready=info.memory_ready,
        vector_dir=info.vector_dir,
    )
