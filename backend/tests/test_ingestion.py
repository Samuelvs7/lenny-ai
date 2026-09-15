"""Ingestion: parsing, sponsor removal, chunking.

Several of these are **regression tests for bugs found against the real
corpus**, not hypotheticals. Each one is labelled with what actually went
wrong, because that is the context a future maintainer needs before "simplifying"
the heuristic back into the bug.
"""

from __future__ import annotations

from app.ingestion.chunk import chunk_blocks
from app.ingestion.parse import TranscriptBlock, parse_transcript, timestamp_to_seconds
from app.ingestion.sponsors import MAX_REMOVAL_FRACTION, strip_sponsor_blocks

TIMESTAMPED = """---
guest: Brian Chesky
title: Brian Chesky's new playbook
youtube_url: https://www.youtube.com/watch?v=4ef0juAMqoE
publish_date: 2023-11-12
duration_seconds: 4408.0
keywords:
- growth
- leadership
---

# Brian Chesky's new playbook

## Transcript

Brian Chesky (00:00:00):
Way too many founders apologize for how they want to run the company and that is a mistake.

Lenny (00:01:01):
Today my guest is Brian Chesky, the CEO and co-founder of Airbnb, a company I know well.

(00:01:27):
In our conversation, Brian shares an in-depth explanation of what is happening with product management, after a short word from our sponsors.

(00:02:17):
This episode is brought to you by Sidebar. Are you looking to land your next big career move? Sidebar matches senior leaders with vetted peer groups for unbiased feedback.

(00:03:06):
Guided by world-class programming, Sidebar enables you to get tactical feedback. Visit sidebar.com/lenny to learn more and jump the waitlist today.

(00:05:00):
Brian, thank you so much for being here. Welcome to the podcast.

Brian Chesky (00:05:04):
Thank you for having me. I am glad we could finally do this properly.
"""

UNTIMESTAMPED = """---
guest: Adriel Frederick
title: Humanizing product development
youtube_url: https://www.youtube.com/watch?v=abc123
---

# Humanizing product development

## Transcript

Adriel Frederick:
There are probably techno utopians who would say feed all the data to the algorithm and let it decide.

Lenny:
Welcome to Lenny's Podcast. I am Lenny and my goal is to help you build better products every week.

Lenny:
This episode is brought to you by Linear. The issue tracker you are using today is not very helpful. Try it for free at linear.app today.

Adriel Frederick:
The thing I learned at Lyft is that marketplaces are fundamentally about trust between two sides.
"""


class TestParsing:
    def test_parses_frontmatter(self):
        parsed = parse_transcript("brian-chesky", TIMESTAMPED)
        assert parsed.guest == "Brian Chesky"
        assert parsed.title == "Brian Chesky's new playbook"
        assert parsed.youtube_url == "https://www.youtube.com/watch?v=4ef0juAMqoE"
        assert parsed.publish_date is not None
        assert parsed.publish_date.year == 2023
        assert "growth" in parsed.keywords
        assert parsed.int_field("duration_seconds") == 4408

    def test_carries_speaker_across_unlabelled_blocks(self):
        """`(00:01:27):` continues the previous speaker.

        Without this, citations for continuation blocks have no attribution —
        and most of Lenny's monologue is continuation blocks.
        """
        parsed = parse_transcript("brian-chesky", TIMESTAMPED)
        by_stamp = {block.start_seconds: block for block in parsed.blocks}
        assert by_stamp[87].speaker == "Lenny"
        assert by_stamp[87].is_labelled is False
        assert by_stamp[0].speaker == "Brian Chesky"
        assert by_stamp[0].is_labelled is True

    def test_parses_timestamp_free_format(self):
        """REGRESSION: a second transcript format exists in this corpus.

        Episodes using bare `Speaker:` labels produced *zero* blocks, so the
        parser fell back to one block for the whole episode — which the sponsor
        detector then deleted in its entirety (100% content loss).
        """
        parsed = parse_transcript("adriel-frederick", UNTIMESTAMPED)
        assert len(parsed.blocks) >= 4
        assert parsed.blocks[0].speaker == "Adriel Frederick"
        assert parsed.blocks[0].is_labelled is True
        assert all(block.start_seconds is None for block in parsed.blocks)

    def test_timestamp_conversion(self):
        assert timestamp_to_seconds("01:13:28") == 4408
        assert timestamp_to_seconds("00:02:17") == 137
        assert timestamp_to_seconds("12:34") == 754
        assert timestamp_to_seconds("nonsense") is None

    def test_missing_frontmatter_still_parses(self):
        """A malformed header must not cost us the episode."""
        parsed = parse_transcript("x", "Lenny (00:00:01):\nJust some content here to index.")
        assert parsed.blocks
        assert parsed.title  # falls back to a slug-derived title


class TestSponsorRemoval:
    def test_removes_ad_read_and_its_continuation(self):
        parsed = parse_transcript("brian-chesky", TIMESTAMPED)
        kept, report = strip_sponsor_blocks(parsed.blocks, guest=parsed.guest)

        text = " ".join(block.text for block in kept)
        assert "brought to you by" not in text.lower()
        assert "sidebar.com/lenny" not in text.lower()
        assert report.segments_removed == 1
        assert "Sidebar" in (report.brands or [])

    def test_keeps_genuine_intro_containing_sponsor_transition(self):
        """REGRESSION: 'after a short word from our sponsors' is not an ad.

        That phrase ends a *genuine* host intro block. Treating it as an ad
        marker deleted real episode content.
        """
        parsed = parse_transcript("brian-chesky", TIMESTAMPED)
        kept, _ = strip_sponsor_blocks(parsed.blocks, guest=parsed.guest)
        text = " ".join(block.text for block in kept)
        assert "In our conversation, Brian shares" in text

    def test_keeps_interview_after_the_ad(self):
        parsed = parse_transcript("brian-chesky", TIMESTAMPED)
        kept, _ = strip_sponsor_blocks(parsed.blocks, guest=parsed.guest)
        text = " ".join(block.text for block in kept)
        assert "Welcome to the podcast" in text
        assert "Thank you for having me" in text

    def test_removes_host_labelled_ad_read(self):
        """REGRESSION: the host reads ads under his own speaker label.

        The original rule 'a labelled turn is never an ad' let every
        `Lenny:`-labelled read straight into the index.
        """
        parsed = parse_transcript("adriel-frederick", UNTIMESTAMPED)
        kept, report = strip_sponsor_blocks(parsed.blocks, guest=parsed.guest)
        text = " ".join(block.text for block in kept)
        assert "brought to you by Linear" not in text
        assert report.segments_removed == 1

    def test_never_removes_a_guest_turn(self):
        """Guests plug their own books and sites. That is real content."""
        blocks = [
            TranscriptBlock(
                speaker="April Dunford",
                start_seconds=100,
                text=(
                    "You can find my book at aprildunford.com and I run a free trial "
                    "of the workshop, so sign up today if you want to learn positioning."
                ),
                is_labelled=True,
            )
        ]
        kept, report = strip_sponsor_blocks(blocks, guest="April Dunford")
        assert len(kept) == 1
        assert report.segments_removed == 0

    def test_safety_rail_aborts_excessive_removal(self):
        """REGRESSION: a parser gap once classified 100% of an episode as ads.

        If detection wants more than the threshold, keep the whole episode. An
        indexed ad is a nuisance; a silently deleted episode is data loss.
        """
        blocks = [
            TranscriptBlock(
                speaker=None,
                start_seconds=index * 10,
                text="This episode is brought to you by Acme. Visit acme.com/lenny to sign up today.",
                is_labelled=False,
            )
            for index in range(5)
        ]
        kept, report = strip_sponsor_blocks(blocks)
        assert len(kept) == len(blocks), "safety rail should keep everything"
        assert report.segments_removed == 0

    def test_removal_stays_within_expected_bounds(self):
        parsed = parse_transcript("brian-chesky", TIMESTAMPED)
        total = sum(len(block.text.split()) for block in parsed.blocks)
        _, report = strip_sponsor_blocks(parsed.blocks, guest=parsed.guest)
        assert 0 < report.words_removed / total < MAX_REMOVAL_FRACTION


class TestChunking:
    def _blocks(self, count: int, words: int = 100) -> list[TranscriptBlock]:
        return [
            TranscriptBlock(
                speaker="Guest" if index % 2 else "Lenny",
                start_seconds=index * 30,
                text=" ".join(f"word{index}x{n}" for n in range(words)),
                is_labelled=True,
            )
            for index in range(count)
        ]

    def test_respects_target_size(self):
        chunks = chunk_blocks(self._blocks(10), target_words=300, overlap_words=50)
        assert chunks
        assert all(chunk.word_count <= 450 for chunk in chunks)

    def test_carries_speaker_and_start_time(self):
        chunks = chunk_blocks(self._blocks(6), target_words=250, overlap_words=40)
        assert chunks[0].speaker == "Lenny"
        assert chunks[0].start_seconds == 0

    def test_indexes_are_sequential(self):
        chunks = chunk_blocks(self._blocks(12), target_words=200, overlap_words=30)
        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))

    def test_splits_a_very_long_single_turn(self):
        long_turn = [
            TranscriptBlock(
                speaker="Guest",
                start_seconds=0,
                text=". ".join(f"Sentence number {n} with some content" for n in range(200)),
                is_labelled=True,
            )
        ]
        chunks = chunk_blocks(long_turn, target_words=200, overlap_words=30)
        assert len(chunks) > 1, "a 1,400-word monologue must not become one chunk"

    def test_drops_trivially_short_content(self):
        tiny = [TranscriptBlock(speaker="Lenny", start_seconds=0, text="Yeah.", is_labelled=True)]
        assert chunk_blocks(tiny, target_words=300, overlap_words=50) == []

    def test_empty_input(self):
        assert chunk_blocks([], target_words=300, overlap_words=50) == []
