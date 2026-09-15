"""Transcript parsing.

Source format (github.com/ChatPRD/lennys-podcast-transcripts), verified against
real episodes::

    ---
    guest: Brian Chesky
    title: Brian Chesky's new playbook
    youtube_url: https://www.youtube.com/watch?v=4ef0juAMqoE
    publish_date: 2023-11-12
    ...
    ---

    # Brian Chesky's new playbook

    ## Transcript

    Brian Chesky (00:00:00):
    Way too many founders apologize for...

    Lenny (00:01:01):
    Today my guest is Brian Chesky...

    (00:01:27):
    In our conversation, Brian shares...

Two block shapes matter:

* **Labelled** — ``Speaker (HH:MM:SS):`` starts a new speaker turn.
* **Unlabelled** — ``(HH:MM:SS):`` continues the *previous* speaker.

Carrying the speaker forward across unlabelled blocks is what lets a citation
say who said something, and the timestamp is what makes the citation a
deep link into the video.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import yaml

#: ``Speaker Name (00:12:34):`` — speaker may contain spaces, dots, apostrophes.
_LABELLED_BLOCK = re.compile(
    r"^(?P<speaker>[^\n(]{1,80}?)\s*\((?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\):\s*$",
    re.MULTILINE,
)
#: ``(00:12:34):`` — continuation of the previous speaker.
_UNLABELLED_BLOCK = re.compile(
    r"^\((?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\):\s*$",
    re.MULTILINE,
)
#: ``Adriel Frederick:`` — a second format in this corpus, with no timestamps
#: at all. Roughly a third of episodes use it. Missing it is not cosmetic: the
#: parser found no blocks, fell back to treating the whole episode as one
#: block, and the sponsor detector then deleted the entire transcript because
#: that single block contained one ad marker. Excludes lines containing '(' so
#: it can never collide with the timestamped forms above.
_NAMED_BLOCK = re.compile(
    r"^(?P<speaker>[A-Z][^\n:(]{0,58}):\s*$",
    re.MULTILINE,
)


@dataclass(slots=True)
class TranscriptBlock:
    """One timestamped passage of speech."""

    speaker: str | None
    start_seconds: int | None
    text: str
    #: True when the source line carried an explicit speaker name. Used by the
    #: sponsor detector: the guest never reads ads, so an explicitly labelled
    #: turn is strong evidence the interview has resumed.
    is_labelled: bool = False


@dataclass(slots=True)
class ParsedTranscript:
    slug: str
    metadata: dict[str, Any]
    blocks: list[TranscriptBlock] = field(default_factory=list)

    # --- convenience accessors over frontmatter -----------------------------

    @property
    def title(self) -> str:
        return str(self.metadata.get("title") or self.slug.replace("-", " ").title())

    @property
    def guest(self) -> str | None:
        value = self.metadata.get("guest")
        return str(value) if value else None

    @property
    def youtube_url(self) -> str | None:
        value = self.metadata.get("youtube_url")
        return str(value) if value else None

    @property
    def video_id(self) -> str | None:
        value = self.metadata.get("video_id")
        return str(value) if value else None

    @property
    def publish_date(self) -> date | None:
        value = self.metadata.get("publish_date")
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError:
                return None
        return None

    @property
    def keywords(self) -> list[str]:
        value = self.metadata.get("keywords")
        if isinstance(value, list):
            return [str(item) for item in value if item]
        return []

    @property
    def description(self) -> str | None:
        value = self.metadata.get("description")
        return " ".join(str(value).split()) if value else None

    def int_field(self, key: str) -> int | None:
        value = self.metadata.get(key)
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def timestamp_to_seconds(stamp: str) -> int | None:
    """``'01:13:28'`` -> ``4408``. Also accepts ``MM:SS``."""
    parts = stamp.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours, minutes, seconds = 0, numbers[0], numbers[1]
    else:
        return None
    return hours * 3600 + minutes * 60 + seconds


def split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Separate YAML frontmatter from the body.

    Returns ``({}, raw)`` when there is no frontmatter rather than raising —
    a transcript with a malformed header is still worth indexing, just with
    less metadata, and failing the whole ingestion over one bad file would be
    the wrong trade.
    """
    if not raw.startswith("---"):
        return {}, raw

    # Find the closing delimiter on its own line.
    end = re.search(r"^---\s*$", raw[3:], re.MULTILINE)
    if end is None:
        return {}, raw

    header = raw[3 : 3 + end.start()]
    body = raw[3 + end.end() :]
    try:
        metadata = yaml.safe_load(header) or {}
    except yaml.YAMLError:
        return {}, body
    if not isinstance(metadata, dict):
        return {}, body
    return metadata, body


def parse_transcript(slug: str, raw: str) -> ParsedTranscript:
    """Parse one ``transcript.md`` into metadata plus ordered blocks."""
    metadata, body = split_frontmatter(raw)

    # Drop the markdown heading and the '## Transcript' marker so they do not
    # become a retrievable chunk of their own.
    body = re.sub(r"^#{1,6}\s+.*$", "", body, flags=re.MULTILINE)

    blocks = _parse_blocks(body)
    return ParsedTranscript(slug=slug, metadata=metadata, blocks=blocks)


def _parse_blocks(body: str) -> list[TranscriptBlock]:
    """Split the body on block headers, carrying the speaker forward."""
    headers: list[tuple[int, int, str | None, str]] = []

    for match in _LABELLED_BLOCK.finditer(body):
        speaker = match.group("speaker").strip()
        # Guard against a stray parenthetical mid-paragraph being read as a
        # speaker label: real labels are short names, not sentences.
        if len(speaker) > 60 or speaker.endswith((".", "?", "!")):
            continue
        headers.append((match.start(), match.end(), speaker, match.group("ts")))

    for match in _UNLABELLED_BLOCK.finditer(body):
        headers.append((match.start(), match.end(), None, match.group("ts")))

    # Timestamp-free format. Only consulted when the timestamped patterns found
    # nothing, so a stray "Note:" line inside a normal episode can never be
    # mistaken for a speaker label.
    if not headers:
        for match in _NAMED_BLOCK.finditer(body):
            speaker = match.group("speaker").strip()
            if not speaker or len(speaker.split()) > 4:
                continue
            headers.append((match.start(), match.end(), speaker, ""))

    if not headers:
        # No recognisable structure — treat the whole body as one block so the
        # episode is still searchable.
        text = " ".join(body.split())
        return [TranscriptBlock(speaker=None, start_seconds=None, text=text)] if text else []

    headers.sort(key=lambda item: item[0])

    blocks: list[TranscriptBlock] = []
    current_speaker: str | None = None

    for index, (_start, header_end, speaker, stamp) in enumerate(headers):
        text_end = headers[index + 1][0] if index + 1 < len(headers) else len(body)
        text = " ".join(body[header_end:text_end].split())
        if not text:
            continue

        is_labelled = speaker is not None
        if is_labelled:
            current_speaker = speaker

        blocks.append(
            TranscriptBlock(
                speaker=current_speaker,
                start_seconds=timestamp_to_seconds(stamp) if stamp else None,
                text=text,
                is_labelled=is_labelled,
            )
        )

    return blocks
