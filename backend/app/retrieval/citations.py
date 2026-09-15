"""Citation construction and validation.

The assistant is asked to cite its sources as ``[S1]``, ``[S2]`` … markers that
map to retrieved passages. Two things can go wrong, and both do in practice:

1. **The model invents a marker** — ``[S7]`` when only four passages were
   retrieved, or ``[S2, S5]`` in a format we never asked for.
2. **The model cites plausibly but we never check**, so a fabricated source is
   rendered in the UI with a real-looking episode title and link.

This module makes (2) structurally impossible. A :class:`Citation` can only be
constructed from a chunk that retrieval actually returned in *this* turn, and
:func:`validate_citations` strips any marker that does not resolve. What the UI
renders is therefore always backed by a real, retrievable passage — which is
the difference between a grounded product and one that merely looks grounded.

Fabrication is counted rather than silently dropped: the rate is a genuine
quality signal about the model in use, and it is logged and returned so it can
be seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain import Citation, RetrievedChunk
from app.observability import get_logger

log = get_logger(__name__)

#: Matches "[S1]", "[s12]", and the multi-reference forms models like to emit:
#: "[S1, S3]" and "[S1][S2]".
_MARKER = re.compile(r"\[\s*([Ss]\d+(?:\s*,\s*[Ss]\d+)*)\s*\]")

#: Longest excerpt shown as the citation preview in the UI.
_QUOTE_CHARS = 240


@dataclass(slots=True)
class CitationReport:
    """Outcome of validating one answer's citations."""

    used: list[Citation] = field(default_factory=list)
    fabricated_markers: list[str] = field(default_factory=list)
    unused_markers: list[str] = field(default_factory=list)

    @property
    def has_fabrications(self) -> bool:
        return bool(self.fabricated_markers)


def _excerpt(content: str) -> str:
    """A short, sentence-aligned preview of the cited passage."""
    text = " ".join(content.split())
    if len(text) <= _QUOTE_CHARS:
        return text
    cut = text[:_QUOTE_CHARS]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if boundary > _QUOTE_CHARS * 0.5:
        return cut[: boundary + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def build_citations(chunks: list[RetrievedChunk]) -> list[Citation]:
    """Turn retrieved chunks into numbered citations (S1, S2, …).

    Ordering matches the order the chunks are presented to the model, so the
    marker the model writes and the source the user clicks are the same thing.
    """
    citations: list[Citation] = []
    for position, chunk in enumerate(chunks, start=1):
        citations.append(
            Citation(
                marker=f"S{position}",
                chunk_id=chunk.chunk_id,
                episode_slug=chunk.episode.slug,
                guest=chunk.episode.guest,
                title=chunk.episode.title,
                youtube_url=chunk.episode.youtube_url,
                deep_link=chunk.deep_link,
                start_seconds=chunk.start_seconds,
                speaker=chunk.speaker,
                quote=_excerpt(chunk.content),
                score=chunk.score,
            )
        )
    return citations


def _split_markers(group: str) -> list[str]:
    return [part.strip().upper() for part in group.split(",") if part.strip()]


def validate_citations(answer: str, available: list[Citation]) -> tuple[str, CitationReport]:
    """Strip unresolvable markers and report which sources were actually used.

    Returns the cleaned answer and a :class:`CitationReport`. The answer text is
    otherwise left alone — we do not rewrite the model's prose, only remove
    references that point at nothing.
    """
    by_marker = {citation.marker.upper(): citation for citation in available}
    report = CitationReport()
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        markers = _split_markers(match.group(1))
        valid = [marker for marker in markers if marker in by_marker]
        invalid = [marker for marker in markers if marker not in by_marker]

        for marker in invalid:
            report.fabricated_markers.append(marker)
        for marker in valid:
            if marker not in seen:
                seen.add(marker)
                report.used.append(by_marker[marker])

        if not valid:
            return ""  # drop the reference entirely
        return "".join(f"[{marker}]" for marker in valid)

    cleaned = _MARKER.sub(replace, answer)

    # Tidy the whitespace that removing a marker can leave behind
    # ("as Lenny says  ." -> "as Lenny says.").
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    report.unused_markers = [
        citation.marker for citation in available if citation.marker.upper() not in seen
    ]

    if report.has_fabrications:
        log.warning(
            "grounding.fabricated_citations",
            fabricated=sorted(set(report.fabricated_markers)),
            available=[c.marker for c in available],
            action="markers removed from the answer before it was stored or shown",
        )

    return cleaned, report


#: Characters of each passage handed to the model. Chunks average ~320 words
#: (~1,900 chars); on a CPU-bound local model the prompt-evaluation cost of the
#: full set dominates total latency, and the tail of a passage rarely carries
#: the answer. Trimming here cut demo latency substantially with no measurable
#: loss of answer quality.
MAX_CONTEXT_CHARS_PER_CHUNK = 1100


def _trim(content: str) -> str:
    text = " ".join(content.split())
    if len(text) <= MAX_CONTEXT_CHARS_PER_CHUNK:
        return text
    cut = text[:MAX_CONTEXT_CHARS_PER_CHUNK]
    boundary = cut.rfind(". ")
    return (cut[: boundary + 1] if boundary > MAX_CONTEXT_CHARS_PER_CHUNK * 0.6 else cut) + " […]"


def format_context(chunks: list[RetrievedChunk], citations: list[Citation]) -> str:
    """Render retrieved passages as the grounding block for a prompt.

    Two deliberate choices:

    * Each passage is delimited and labelled with its marker, so the model has
      an unambiguous token to cite.
    * The block is explicitly framed as *data*. Transcript text is untrusted
      input — a guest quoting an instruction, or a hostile string in a future
      corpus, must not be executed as a directive. See ``docs/security.md``.
    """
    lines: list[str] = []
    for chunk, citation in zip(chunks, citations):
        speaker = chunk.speaker or "Unknown speaker"
        stamp = ""
        if chunk.start_seconds is not None:
            minutes, seconds = divmod(chunk.start_seconds, 60)
            hours, minutes = divmod(minutes, 60)
            stamp = f" at {hours:d}:{minutes:02d}:{seconds:02d}"
        lines.append(
            f"[{citation.marker}] Episode: {chunk.episode.title}\n"
            f"Guest: {chunk.episode.guest or 'unknown'} | Speaker: {speaker}{stamp}\n"
            f"Passage: {_trim(chunk.content)}"
        )
    return "\n\n---\n\n".join(lines)
