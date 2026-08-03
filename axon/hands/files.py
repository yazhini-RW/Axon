"""The files & docs hand: create a new file freely (safe), only overwrite or delete an
existing one after approval (risky).

See docs/V2-PLAN.md Step 15. Fully local, no external account or key needed at all —
the simplest hand in V3. The safe/risky boundary here isn't "stays local vs leaves the
machine" (nothing here ever leaves the machine) — it's "creates something new" (safe,
nothing to lose) vs "destroys something that already exists" (risky, real data loss).

Axon never silently overwrites a file just because a new note happens to pick the same
name — see `_unique_path()`. It will only ever overwrite or delete an existing file
when the note explicitly says so ("overwrite", "replace", "delete" — already in
`_RISKY_VERBS`, so these notes are already flagged risky and pause for approval before
`execute()` can touch anything).
"""

from __future__ import annotations

import re
from pathlib import Path

from axon.config import Settings, get_settings
from axon.models import ApprovalOutcome, ClassifiedNote, PreparedNote

_FILE_WORDS = {"file", "document", "doc", "docs"}
_WRITE_VERBS = {"write", "create", "save", "draft", "make"}
_DESTRUCTIVE_VERBS = {"overwrite", "replace", "delete"}
_FILENAME_PATTERN = re.compile(r"\b[\w-]+\.(?:txt|md|csv|json)\b")

_LEAD_IN = re.compile(
    r"^(please\s+)?(overwrite|replace|delete|write|create|save|draft|make)\s+"
    r"(a\s+|the\s+|new\s+)*(file|document|doc)?\s*(called\s+|named\s+|titled\s+)?",
    re.IGNORECASE,
)
# Handles phrasing where the filename comes last ("save the list to groceries.txt") —
# after the filename is removed, "to" is left dangling at the end. Same trap as the
# email hand's _TRAILING_TO.
_TRAILING_TO = re.compile(r"\s+to\s*$", re.IGNORECASE)
# Handles phrasing where the filename comes right after the verb ("overwrite
# groceries.txt with the final list") — once the filename is gone, "with"/"as" is
# left dangling at the start of what should be the content.
_LEADING_CONNECTOR = re.compile(r"^(with|as)\s+", re.IGNORECASE)


def looks_like_a_files_note(text: str) -> bool:
    lowered = text.lower()
    words = set(re.findall(r"[a-z]+", lowered))
    mentions_a_file = bool(words & _FILE_WORDS) or bool(_FILENAME_PATTERN.search(text))
    has_a_verb = bool(words & (_WRITE_VERBS | _DESTRUCTIVE_VERBS))
    return mentions_a_file and has_a_verb


def _is_destructive(text: str) -> bool:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return bool(words & _DESTRUCTIVE_VERBS)


def _slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    skip = _FILE_WORDS | _WRITE_VERBS | _DESTRUCTIVE_VERBS | {
        "a", "an", "the", "to", "for", "and", "on", "my", "this", "called", "named", "titled",
    }
    slug = "-".join(w for w in words if w not in skip)[:40].strip("-")
    return slug or "note"


def _filename_and_content(text: str) -> tuple[str, str]:
    """Pull (filename, content) out of the note text. Deterministic, no AI — same
    "free by default" pattern as the mock brain, the mock GitHub builder, and the
    email/chat hands' drafting."""
    explicit = _FILENAME_PATTERN.search(text)
    filename = explicit.group(0) if explicit else f"{_slugify(text)}.md"

    body = _FILENAME_PATTERN.sub("", text)
    body = _LEAD_IN.sub("", body).strip()
    body = _LEADING_CONNECTOR.sub("", body).strip()
    body = _TRAILING_TO.sub("", body).strip() or text
    title = filename.rsplit(".", 1)[0].replace("-", " ")
    content = f"# {title}\n\n{body}\n"
    return filename, content


def _unique_path(directory: Path, filename: str) -> Path:
    """Never silently overwrite. If the name's taken and the note didn't ask to
    overwrite/replace it, count up (todo.md, todo-2.md, todo-3.md, ...) instead."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem, _, ext = filename.rpartition(".")
    stem = stem or filename
    counter = 2
    while (directory / f"{stem}-{counter}.{ext}").exists():
        counter += 1
    return directory / f"{stem}-{counter}.{ext}"


class FilesHand:
    """`prepare`: create freely, never clobber. `execute`: overwrite/delete, only
    once approved, and only when the note explicitly asked for that."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        self._settings.documents_dir.mkdir(parents=True, exist_ok=True)
        filename, content = _filename_and_content(note.text)
        target = self._settings.documents_dir / filename

        if _is_destructive(note.text) and target.exists():
            # Never touch the existing file here — that's execute()'s job, and only
            # once approved. Nothing written yet.
            verb = "delete" if "delete" in note.text.lower() else "overwrite"
            return PreparedNote(
                note=note,
                detail=f"would {verb} {target} — needs your OK",
                project_dir=str(target),  # reused field: "the path this acts on"
            )

        safe_target = _unique_path(self._settings.documents_dir, filename)
        safe_target.write_text(content, encoding="utf-8")
        return PreparedNote(
            note=note,
            detail=f"wrote {safe_target}",
            project_dir=str(safe_target),
        )

    async def execute(self, outcome: ApprovalOutcome) -> None:
        """The risky half. Only ever called with an approved outcome — see
        ExecuteExecutor.run_execute in axon/brain/workflow.py.

        Re-derives the filename/content from the note rather than trusting anything
        carried in-memory from prepare(), same reasoning as every other hand: this can
        run in a fresh process after a resume.
        """
        note = outcome.note
        if not _is_destructive(note.text):
            return  # prepare() already did the (safe) write; nothing left to do

        filename, content = _filename_and_content(note.text)
        target = self._settings.documents_dir / filename

        words = set(re.findall(r"[a-z]+", note.text.lower()))
        if "delete" in words and target.exists():
            target.unlink()
        else:
            self._settings.documents_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
