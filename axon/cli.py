"""The `axon` command line."""

from __future__ import annotations

import asyncio
import sys

import typer
from rich.console import Console
from rich.table import Table

from axon.config import get_settings
from axon.db import repo
from axon.models import ClassifiedNote, Note, NoteKind

def _use_utf8() -> None:
    """The Windows console defaults to cp1252, which turns an em-dash into a black
    diamond. Notes are free text, so this protects whatever the user actually typed."""
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", "") or ""
        if encoding.lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass  # not a real terminal; rich will cope


_use_utf8()

app = typer.Typer(
    add_completion=False,
    help="A second brain that remembers your notes and acts on them.",
)
console = Console()

_KIND_STYLE = {
    NoteKind.UNCLASSIFIED: "dim",
    NoteKind.FACT: "cyan",
    NoteKind.TASK: "yellow",
    NoteKind.REMINDER: "magenta",
}


def _format_due(note: Note) -> str:
    local = note.due_at_local
    return local.strftime("%a %d %b %H:%M") if local else "-"


@app.command()
def add(text: str = typer.Argument(..., help="The note, in your own words.")) -> None:
    """Capture a note and work out what it is."""
    with repo.open_db() as conn:
        # Write it down first. Whatever the brain does next, the note is safe.
        try:
            note = repo.add_note(conn, text)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        console.print(f"[green]captured[/green] #{note.id}  {note.text}")

        def persist(result: ClassifiedNote) -> None:
            repo.apply_classification(
                conn,
                result.note_id,
                result.classification.kind,
                result.classification.due_at,
            )

        # Imported here, not at module level: `axon list` and `axon doctor` never
        # classify or remember anything and shouldn't wait for these.
        from axon.brain.workflow import run_capture
        from axon.memory.store import MemoryLocked, MemoryStore

        try:
            with console.status("[dim]thinking...[/dim]"), MemoryStore() as memory:

                def remember(result: ClassifiedNote) -> None:
                    memory.remember(
                        result.text, note_id=result.note_id, kind=result.classification.kind.value
                    )

                classified = asyncio.run(run_capture(note.id, note.text, persist, remember))
        except MemoryLocked as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(code=1) from exc
        except Exception as exc:  # the note is already saved; never lose it over this
            console.print(f"[yellow]couldn't classify it just now:[/yellow] {exc}")
            console.print("[dim]it's saved as unclassified — try `axon list`[/dim]")
            raise typer.Exit(code=1) from exc

    verdict = classified.classification
    style = _KIND_STYLE[verdict.kind]
    console.print(f"  -> [{style}]{verdict.kind.value}[/]  [dim]({verdict.reason})[/dim]")

    if verdict.due_at:
        local = verdict.due_at.astimezone()
        console.print(f"  -> due [bold]{local.strftime('%a %d %b %Y at %H:%M')}[/bold]")
    if verdict.recurring:
        console.print("  -> [yellow]repeats[/yellow] [dim](V1 fires once — recurrence comes later)[/dim]")
    if verdict.risky:
        console.print("  -> [red]risky[/red] [dim](will need your OK before Axon acts)[/dim]")


@app.command("list")
def list_cmd(
    limit: int = typer.Option(20, "--limit", "-n", help="How many to show."),
) -> None:
    """Show your most recent notes."""
    with repo.open_db() as conn:
        notes = repo.list_notes(conn, limit=limit)
        total = repo.count_notes(conn)

    if not notes:
        console.print("[dim]nothing captured yet — try: axon add \"buy milk at 5pm\"[/dim]")
        return

    table = Table(title=f"{len(notes)} of {total} notes", title_justify="left")
    table.add_column("#", justify="right", style="dim")
    table.add_column("note")
    table.add_column("kind")
    table.add_column("due")
    table.add_column("status", style="dim")

    for note in notes:
        table.add_row(
            str(note.id),
            note.text,
            f"[{_KIND_STYLE[note.kind]}]{note.kind.value}[/]",
            _format_due(note),
            note.status.value,
        )
    console.print(table)


# Similarity scores from this model bunch up in a narrow band (roughly 0.4-0.7), so an
# absolute cutoff either hides correct answers or shows everything. These compare
# results against each other instead, which is what actually carries information.
_NOTHING_RELATED = 0.30   # below this, the best hit is not about the query at all
_RELATIVE_BAND = 0.15     # how far behind the winner a result can be and still be shown
_TOO_CLOSE_TO_CALL = 0.05  # if 1st and 2nd are this close, Axon is guessing


@app.command()
def recall(
    query: str = typer.Argument(..., help="What you're trying to remember."),
    limit: int = typer.Option(5, "--limit", "-n", help="How many to consider."),
) -> None:
    """Search your memory by meaning, not by exact words."""
    from axon.memory.store import MemoryLocked, MemoryStore

    try:
        with console.status("[dim]remembering...[/dim]"), MemoryStore() as memory:
            hits = memory.recall(query, limit=limit)
    except MemoryLocked as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=1) from exc

    if not hits:
        console.print("[dim]nothing in memory yet[/dim]")
        return

    best = hits[0].score
    if best < _NOTHING_RELATED:
        console.print("[dim]nothing in memory looks related to that[/dim]")
        return

    shown = [h for h in hits if h.score >= best - _RELATIVE_BAND]
    runner_up = hits[1].score if len(hits) > 1 else 0.0
    unsure = len(shown) > 1 and (best - runner_up) < _TOO_CLOSE_TO_CALL

    table = Table(box=None, pad_edge=False)
    table.add_column("", style="dim")
    table.add_column("note")
    table.add_column("kind")

    for index, hit in enumerate(shown):
        marker = "[green]closest[/green]" if index == 0 and not unsure else "[dim]also[/dim]"
        note_id = f"[dim]#{hit.note_id}[/dim] " if hit.note_id else ""
        table.add_row(
            f"{marker} [dim]{hit.score:.2f}[/dim]",
            f"{note_id}{hit.text}",
            f"[dim]{hit.kind or '-'}[/dim]",
        )
    console.print(table)

    if unsure:
        console.print(
            "\n[yellow]these all look about equally similar[/yellow]"
            " [dim]- Axon isn't confident which you meant[/dim]"
        )


@app.command()
def doctor() -> None:
    """Show how Axon is configured and whether anything costs money."""
    settings = get_settings()
    with repo.open_db() as conn:
        total = repo.count_notes(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    brain = (
        "[green]mock[/green] (rule-based, free, offline)"
        if settings.brain_mode == "mock"
        else "[cyan]gemini[/cyan] (free tier)"
    )

    table = Table(show_header=False, box=None)
    table.add_row("brain", brain)
    table.add_row("embeddings", f"{settings.embed_model} [dim](local, no key)[/dim]")
    table.add_row("data dir", str(settings.data_dir))
    table.add_row("database", f"{settings.db_path} [dim](schema v{version})[/dim]")
    table.add_row("notes stored", str(total))
    # Deliberately not opened here: that would cost ~5s and take the storage lock.
    memory_state = (
        "ready" if settings.vector_dir.exists() else "[dim]not built yet (first `axon add`)[/dim]"
    )
    table.add_row("memory", f"{memory_state} [dim]{settings.vector_dir}[/dim]")
    console.print(table)
    console.print("\n[dim]nothing here requires a paid service.[/dim]")


if __name__ == "__main__":
    app()
