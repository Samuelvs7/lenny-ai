"""Chunking transcript blocks into retrievable passages.

Design choices and why:

* **Chunk on speaker-turn boundaries, not a fixed character window.** A turn is
  a complete thought. Cutting mid-sentence produces chunks that retrieve well
  but read badly when quoted back as a citation, and the citation is the part
  the user is asked to trust.
* **Carry speaker and start timestamp from the first block in the chunk.** That
  is what makes ``youtube_url&t=NNNs`` land on the moment the passage begins.
* **Overlap by whole blocks.** A question answered across a turn boundary
  ("...and that's why we did X" / "X being the pricing change") stays
  retrievable from either side.
* **Split oversized single turns by sentence.** Some guests talk for four
  minutes straight; one 900-word chunk dilutes its own embedding and retrieves
  poorly. Rare, but it happens in this corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.parse import TranscriptBlock

#: Chunks shorter than this carry too little meaning to embed usefully — they
#: are mostly interjections ("Yeah, exactly.") that match everything weakly.
MIN_CHUNK_WORDS = 25

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class Chunk:
    """A passage ready to embed, index, and cite."""

    index: int
    content: str
    speaker: str | None
    start_seconds: int | None
    end_seconds: int | None
    word_count: int


def _split_long_block(block: TranscriptBlock, max_words: int) -> list[str]:
    """Break one very long turn into sentence-aligned pieces."""
    sentences = _SENTENCE_BOUNDARY.split(block.text)
    pieces: list[str] = []
    current: list[str] = []
    current_words = 0

    for sentence in sentences:
        words = len(sentence.split())
        if current and current_words + words > max_words:
            pieces.append(" ".join(current))
            current = [sentence]
            current_words = words
        else:
            current.append(sentence)
            current_words += words

    if current:
        pieces.append(" ".join(current))
    return [piece for piece in pieces if piece.strip()]


def chunk_blocks(
    blocks: list[TranscriptBlock],
    *,
    target_words: int = 320,
    overlap_words: int = 60,
    duration_seconds: int | None = None,
) -> list[Chunk]:
    """Group blocks into chunks of roughly ``target_words``.

    ``duration_seconds`` (from episode frontmatter) bounds the final chunk's
    end timestamp, so a citation never claims a passage runs past the end of
    the episode.
    """
    if not blocks:
        return []

    # Expand any single block that is far over target into sentence pieces,
    # keeping the original speaker and timestamp on each piece.
    expanded: list[TranscriptBlock] = []
    oversize_limit = int(target_words * 1.5)
    for block in blocks:
        if len(block.text.split()) > oversize_limit:
            for piece in _split_long_block(block, target_words):
                expanded.append(
                    TranscriptBlock(
                        speaker=block.speaker,
                        start_seconds=block.start_seconds,
                        text=piece,
                        is_labelled=block.is_labelled,
                    )
                )
        else:
            expanded.append(block)

    chunks: list[Chunk] = []
    window: list[TranscriptBlock] = []
    window_words = 0
    chunk_index = 0

    def flush(next_start: int | None) -> list[TranscriptBlock]:
        """Emit the current window as a chunk; return the overlap tail."""
        nonlocal chunk_index, window_words
        if not window:
            return []

        content = " ".join(block.text for block in window).strip()
        word_count = len(content.split())
        if word_count >= MIN_CHUNK_WORDS:
            end = next_start
            if end is None:
                end = duration_seconds
            chunks.append(
                Chunk(
                    index=chunk_index,
                    content=content,
                    speaker=window[0].speaker,
                    start_seconds=window[0].start_seconds,
                    end_seconds=end,
                    word_count=word_count,
                )
            )
            chunk_index += 1

        # Build the overlap tail from the end of the window.
        tail: list[TranscriptBlock] = []
        tail_words = 0
        for block in reversed(window):
            block_words = len(block.text.split())
            if tail_words + block_words > overlap_words and tail:
                break
            tail.insert(0, block)
            tail_words += block_words
        window_words = tail_words
        return tail

    for position, block in enumerate(expanded):
        block_words = len(block.text.split())

        if window and window_words + block_words > target_words:
            next_start = block.start_seconds
            window = flush(next_start)

        window.append(block)
        window_words += block_words

        is_last = position == len(expanded) - 1
        if is_last:
            flush(None)
            window = []

    return chunks
