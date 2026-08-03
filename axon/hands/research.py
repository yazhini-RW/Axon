"""The research hand: look something up and summarise it. Read-only — no risky half.

See docs/V2-PLAN.md Step 16. Free via DuckDuckGo's no-key Instant Answer API. Confirmed
against the real API, not assumed: it only returns something for well-known topics with
a Wikipedia-style abstract or a direct dictionary definition — current events, obscure
queries, and even simple calculations ("15 percent of 200") all came back empty when
tested for real. That limit is stated plainly here and in what Axon shows you, not
hidden — same as the mock brain and the recall confidence bands.

There is no `execute` half worth having: looking something up never leaves a trace or
changes anything, so `prepare` does the whole job and `execute` is a deliberate no-op.
"""

from __future__ import annotations

import re

import httpx

from axon.models import ApprovalOutcome, ClassifiedNote, PreparedNote

_TRIGGER_PHRASES = (
    "research", "look up", "find out", "what is", "what are",
    "who is", "who are", "summarize", "summarise",
)

_LEAD_IN = re.compile(
    r"^(please\s+)?(research|look\s+up|find\s+out|what\s+is|what\s+are|"
    r"who\s+is|who\s+are|summarize|summarise)\s+",
    re.IGNORECASE,
)

DUCKDUCKGO_URL = "https://api.duckduckgo.com/"


def looks_like_a_research_note(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _TRIGGER_PHRASES)


def _query(text: str) -> str:
    return _LEAD_IN.sub("", text).strip().rstrip("?") or text


async def _look_up(query: str) -> str:
    """Ask DuckDuckGo's free, no-key Instant Answer API. Returns a short summary, or
    an honest "nothing found" message — never raises for an empty result, since
    finding nothing is a normal outcome here, not a failure."""
    params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    async with httpx.AsyncClient() as client:
        response = await client.get(DUCKDUCKGO_URL, params=params, timeout=10.0)
        response.raise_for_status()
        data = response.json()

    for field in ("Answer", "AbstractText", "Definition"):
        value = (data.get(field) or "").strip()
        if value:
            source = data.get("AbstractSource") or data.get("DefinitionSource")
            suffix = f" (via {source})" if source else ""
            return f"{value}{suffix}"

    return (
        f"no quick answer found for {query!r} — DuckDuckGo's free API only covers "
        "well-known topics with a summary or definition, not full web search or "
        "current events"
    )


class ResearchHand:
    """Read-only. `prepare` does the whole job — a lookup never needs approval.
    `execute` is a deliberate no-op; there is nothing risky to defer."""

    async def prepare(self, note: ClassifiedNote) -> PreparedNote:
        summary = await _look_up(_query(note.text))
        return PreparedNote(note=note, detail=summary)

    async def execute(self, outcome: ApprovalOutcome) -> None:
        return None
