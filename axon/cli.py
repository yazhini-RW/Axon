"""The `axon` command line.

This is one of two front doors to axon.service — axon/web/api.py (Step 10) is the
other. Both call the same functions there; this module's job is only to format results
as rich console output and turn service exceptions into typer.Exit.
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.table import Table

from axon import service
from axon.models import Note, NoteKind, utcnow


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
    try:
        with console.status("[dim]thinking...[/dim]"):
            result = service.add_note(
                text,
                on_captured=lambda note: console.print(
                    f"[green]captured[/green] #{note.id}  {note.text}"
                ),
            )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except service.MemoryLocked as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # the note is already saved; never lose it over this
        console.print(f"[yellow]couldn't classify it just now:[/yellow] {exc}")
        console.print("[dim]it's saved as unclassified — try `axon list`[/dim]")
        raise typer.Exit(code=1) from exc

    if result.pending_approval_id is not None:
        console.print(
            f"  -> [red]needs your OK[/red] to {result.pending_action}"
            f" [dim](this is where Axon always stops and asks)[/dim]"
        )
        if result.pending_detail:
            console.print(f"     [dim]{result.pending_detail}[/dim]")
        if result.pending_push_url:
            console.print(f"     [dim]will run: git push {result.pending_push_url}[/dim]")
        console.print(
            f"     [bold]axon approve {result.pending_approval_id}[/bold]"
            f"  or  [bold]axon reject {result.pending_approval_id}[/bold]"
        )
        return

    verdict = result.completed.note.classification
    style = _KIND_STYLE[verdict.kind]
    console.print(f"  -> [{style}]{verdict.kind.value}[/]  [dim]({verdict.reason})[/dim]")

    if verdict.due_at:
        local = verdict.due_at.astimezone()
        console.print(f"  -> due [bold]{local.strftime('%a %d %b %Y at %H:%M')}[/bold]")
        if verdict.due_at <= utcnow():
            console.print(
                "  -> [yellow]that time has already passed[/yellow]"
                " [dim](it will fire as soon as `axon run` is going)[/dim]"
            )
    if verdict.recurring:
        console.print("  -> [yellow]repeats[/yellow] [dim](V1 fires once — recurrence comes later)[/dim]")


@app.command("list")
def list_cmd(
    limit: int = typer.Option(20, "--limit", "-n", help="How many to show."),
) -> None:
    """Show your most recent notes."""
    notes, total = service.list_notes(limit=limit)

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


@app.command()
def recall(
    query: str = typer.Argument(..., help="What you're trying to remember."),
    limit: int = typer.Option(5, "--limit", "-n", help="How many to consider."),
) -> None:
    """Search your memory by meaning, not by exact words."""
    try:
        with console.status("[dim]remembering...[/dim]"):
            result = service.recall(query, limit=limit)
    except service.MemoryLocked as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=1) from exc

    if not result.hits:
        console.print("[dim]nothing in memory yet[/dim]")
        return
    if not result.shown:
        console.print("[dim]nothing in memory looks related to that[/dim]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("", style="dim")
    table.add_column("note")
    table.add_column("kind")

    for index, hit in enumerate(result.shown):
        marker = "[green]closest[/green]" if index == 0 and not result.unsure else "[dim]also[/dim]"
        note_id = f"[dim]#{hit.note_id}[/dim] " if hit.note_id else ""
        table.add_row(
            f"{marker} [dim]{hit.score:.2f}[/dim]",
            f"{note_id}{hit.text}",
            f"[dim]{hit.kind or '-'}[/dim]",
        )
    console.print(table)

    if result.unsure:
        console.print(
            "\n[yellow]these all look about equally similar[/yellow]"
            " [dim]- Axon isn't confident which you meant[/dim]"
        )


@app.command()
def approvals() -> None:
    """Show what's waiting on your OK. See docs/adr/0003-human-approval-checkpoint.md."""
    pending = service.list_approvals()

    if not pending:
        console.print("[dim]nothing waiting for approval[/dim]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("wants to")
    table.add_column("note")
    table.add_column("exact command", style="dim")

    for approval in pending:
        command = f"git push {approval.push_url}" if approval.push_url else ""
        table.add_row(
            str(approval.id), f"[yellow]{approval.action}[/yellow]", approval.note_text, command
        )
    console.print(table)
    console.print("\n[dim]axon approve <#>  or  axon reject <#>[/dim]")


def _resolve(approval_id: int, approved: bool) -> None:
    """Shared by `approve` and `reject`: resume a paused workflow with the answer.

    This can run in a completely different process than the one that paused it — the
    whole point is that it does not need to. See ADR-0001 and ADR-0003.
    """
    try:
        with console.status("[dim]resuming...[/dim]"):
            result = service.resolve_approval(approval_id, approved)
    except service.ApprovalNotFound:
        console.print(f"[red]no approval #{approval_id}[/red]")
        raise typer.Exit(code=1) from None
    except service.ApprovalAlreadyResolved as exc:
        console.print(f"[yellow]approval #{approval_id} was already {exc.status}[/yellow]")
        raise typer.Exit(code=1) from None
    except service.ApprovalExecutionFailed as exc:
        console.print(f"[red]#{approval_id} failed:[/red] {exc}")
        console.print(
            f"[dim]approval #{approval_id} is still pending — fix the problem and re-approve[/dim]"
        )
        raise typer.Exit(code=1) from None

    verb = "approved" if approved else "rejected"
    colour = "green" if approved else "yellow"
    console.print(
        f"[{colour}]{verb}[/{colour}] #{approval_id}: {result.action} \"{result.note_text}\""
    )
    if result.paused_again:  # not reachable in V1/V2 (one gate), guarded in case that changes
        console.print("[yellow]this paused again — check `axon approvals`[/yellow]")


@app.command()
def approve(approval_id: int = typer.Argument(..., help="From `axon approvals`.")) -> None:
    """Give the OK to a paused, risky action."""
    _resolve(approval_id, approved=True)


@app.command()
def reject(approval_id: int = typer.Argument(..., help="From `axon approvals`.")) -> None:
    """Say no. Axon will not do it."""
    _resolve(approval_id, approved=False)


@app.command()
def run() -> None:
    """Start the scheduler and wait. Reminders fire as notifications."""
    from axon.scheduler.runner import SYNC_SECONDS, ReminderService

    reminder_service = ReminderService()
    counts = reminder_service.start()

    console.print("[green]axon is running[/green] [dim](ctrl-c to stop)[/dim]")
    if counts["fired"]:
        console.print(f"  fired [yellow]{counts['fired']}[/yellow] overdue reminder(s) just now")
    if counts["missed"]:
        console.print(f"  marked [dim]{counts['missed']}[/dim] as missed (more than a day late)")
    console.print(f"  watching [bold]{counts['scheduled']}[/bold] reminder(s)")
    console.print(f"[dim]  checking for new notes every {SYNC_SECONDS}s[/dim]")

    reminder_service.run_forever()
    console.print("\n[dim]stopped[/dim]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8420, help="Bind port."),
) -> None:
    """Start the web backend (Step 10). Serves the same operations over HTTP."""
    import uvicorn

    console.print(f"[green]axon web is running[/green] at [bold]http://{host}:{port}[/bold]")
    console.print("[dim](ctrl-c to stop)[/dim]")
    uvicorn.run("axon.web.api:app", host=host, port=port, log_level="warning")


@app.command()
def doctor() -> None:
    """Show how Axon is configured and whether anything costs money."""
    info = service.doctor_info()

    brain = (
        "[green]mock[/green] (rule-based, free, offline)"
        if info.brain_mode == "mock"
        else "[cyan]gemini[/cyan] (free tier)"
    )

    table = Table(show_header=False, box=None)
    table.add_row("brain", brain)
    table.add_row("embeddings", f"{info.embed_model} [dim](local, no key)[/dim]")
    table.add_row("data dir", info.data_dir)
    table.add_row("database", f"{info.db_path} [dim](schema v{info.schema_version})[/dim]")
    table.add_row("notes stored", str(info.notes_total))
    if info.approvals_pending:
        table.add_row(
            "awaiting your OK", f"[yellow]{info.approvals_pending}[/yellow] — see `axon approvals`"
        )
    # Doesn't open memory itself: that would cost ~5s and take the storage lock.
    memory_state = "ready" if info.memory_ready else "[dim]not built yet (first `axon add`)[/dim]"
    table.add_row("memory", f"{memory_state} [dim]{info.vector_dir}[/dim]")
    console.print(table)
    console.print("\n[dim]nothing here requires a paid service.[/dim]")


if __name__ == "__main__":
    app()
