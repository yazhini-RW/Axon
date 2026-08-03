"""The files & docs hand: create freely (safe), overwrite/delete an existing file only
on approval (risky). See docs/V2-PLAN.md Step 15.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axon.config import Settings
from axon.hands import pick_hand
from axon.hands.files import FilesHand, looks_like_a_files_note
from axon.hands.noop import NoopHand
from axon.models import ApprovalOutcome, Classification, ClassifiedNote, NoteKind


def _note(text: str, note_id: int = 1) -> ClassifiedNote:
    return ClassifiedNote(
        note_id=note_id, text=text,
        classification=Classification(kind=NoteKind.TASK, risky=False, reason="test"),
    )


# --- routing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "write a shopping list to a file",
        "create a document called meeting notes",
        "save this as todo.txt",
        "delete draft.md",
        "overwrite notes.txt with the new plan",
    ],
)
def test_recognises_files_notes(text: str) -> None:
    assert looks_like_a_files_note(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "fix the login bug",
        "buy milk at 5pm",
        "write the quarterly report",  # "write" alone, no file/document word or filename
    ],
)
def test_does_not_misroute_ordinary_notes(text: str) -> None:
    assert looks_like_a_files_note(text) is False


def test_pick_hand_routes_files_notes_to_files_hand(settings: Settings) -> None:
    assert isinstance(pick_hand("save this as todo.txt", settings), FilesHand)


def test_pick_hand_routes_everything_else_to_noop(settings: Settings) -> None:
    assert isinstance(pick_hand("fix the login bug", settings), NoopHand)


# --- prepare: creates freely, never clobbers -----------------------------------------


async def test_prepare_writes_a_new_file(settings: Settings) -> None:
    hand = FilesHand(settings=settings)
    note = _note("save the grocery list to groceries.txt")

    prepared = await hand.prepare(note)

    written = Path(prepared.project_dir)
    assert written.exists()
    assert written.name == "groceries.txt"
    assert "grocery list" in written.read_text(encoding="utf-8")


def test_draft_strips_the_dangling_trailing_to(settings: Settings) -> None:
    """Regression: "save my grocery list to groceries.txt" left "my grocery list to"
    (with a dangling "to") as the file content, since only the filename was stripped,
    not the "to" that introduced it. Found by actually reading the written file."""
    from axon.hands.files import _filename_and_content

    _filename, content = _filename_and_content("save my grocery list to groceries.txt")
    assert "to\n" not in content
    assert content.strip().endswith("my grocery list")


def test_draft_strips_the_dangling_leading_connector(settings: Settings) -> None:
    """Regression: "overwrite groceries.txt with the final list" left "with the final
    list" as the content, since the filename sits right after the verb here, not at
    the end. Found the same way as the trailing-"to" case: by reading the written file."""
    from axon.hands.files import _filename_and_content

    _filename, content = _filename_and_content("overwrite groceries.txt with the final list")
    body = content.split("\n\n", 1)[1]
    assert not body.lower().startswith("with ")
    assert body.strip() == "the final list"


async def test_prepare_never_overwrites_an_existing_file(settings: Settings) -> None:
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    existing = settings.documents_dir / "todo.txt"
    existing.write_text("original content", encoding="utf-8")

    hand = FilesHand(settings=settings)
    note = _note("save my todo list to todo.txt")

    prepared = await hand.prepare(note)

    assert existing.read_text(encoding="utf-8") == "original content", "must never clobber"
    written = Path(prepared.project_dir)
    assert written.name == "todo-2.txt", "picks a non-colliding name instead"
    assert written.exists()


async def test_prepare_does_not_write_when_the_note_is_destructive(settings: Settings) -> None:
    """An overwrite/replace note against an existing file must not touch it in
    prepare() - only execute(), and only once approved, may do that."""
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    existing = settings.documents_dir / "notes.txt"
    existing.write_text("original content", encoding="utf-8")

    hand = FilesHand(settings=settings)
    note = _note("overwrite notes.txt with the new plan")

    prepared = await hand.prepare(note)

    assert existing.read_text(encoding="utf-8") == "original content"
    assert "would overwrite" in prepared.detail


async def test_prepare_says_delete_not_overwrite_for_a_delete_note(settings: Settings) -> None:
    """Regression: found by manually running `delete groceries.txt` through the real
    CLI and seeing "would overwrite" in the pause message - misleading for a delete."""
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    existing = settings.documents_dir / "notes.txt"
    existing.write_text("original content", encoding="utf-8")

    hand = FilesHand(settings=settings)
    note = _note("delete notes.txt")

    prepared = await hand.prepare(note)

    assert "would delete" in prepared.detail
    assert "overwrite" not in prepared.detail


async def test_prepare_writes_freely_when_the_destructive_target_does_not_exist(
    settings: Settings,
) -> None:
    """"overwrite X" where X doesn't exist yet is just... creating X. Nothing to lose."""
    hand = FilesHand(settings=settings)
    note = _note("overwrite plan.txt with the new plan")

    prepared = await hand.prepare(note)

    assert Path(prepared.project_dir).exists()
    assert "would overwrite" not in prepared.detail


# --- execute: only touches existing files, only once approved -----------------------


async def test_execute_overwrites_only_when_approved(settings: Settings) -> None:
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    existing = settings.documents_dir / "notes.txt"
    existing.write_text("original content", encoding="utf-8")

    hand = FilesHand(settings=settings)
    note = _note("overwrite notes.txt with the new plan")
    outcome = ApprovalOutcome(note=note, approved=True)

    await hand.execute(outcome)

    assert existing.read_text(encoding="utf-8") != "original content"
    assert "new plan" in existing.read_text(encoding="utf-8")


async def test_execute_deletes_only_when_approved(settings: Settings) -> None:
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    existing = settings.documents_dir / "draft.md"
    existing.write_text("scratch notes", encoding="utf-8")

    hand = FilesHand(settings=settings)
    note = _note("delete draft.md")
    outcome = ApprovalOutcome(note=note, approved=True)

    await hand.execute(outcome)

    assert not existing.exists()


async def test_execute_is_a_noop_for_non_destructive_notes(settings: Settings) -> None:
    """prepare() already did the (safe) write for a plain create note; execute()
    running afterward (as it does for every approved outcome) must not double-write
    or otherwise touch anything."""
    hand = FilesHand(settings=settings)
    note = _note("save the grocery list to groceries.txt")
    outcome = ApprovalOutcome(note=note, approved=True)

    await hand.execute(outcome)  # must not raise, must not touch the filesystem at all

    assert not settings.documents_dir.exists(), "execute() must be a pure no-op here"
