"""Sponsor / ad-read removal.

**Why this module exists.** Lenny's Podcast transcripts inline the host-read
sponsor segments with the interview. A naive pipeline indexes them, and the
assistant then answers a growth question by quoting an advertisement — e.g.
citing *"visit sidebar.com/lenny"* or a Jira Product Discovery pitch as product
advice from the episode. That is a grounding failure the user would notice
immediately and would rightly not trust.

**How ads are shaped in this corpus** (established by reading real episodes,
not assumed):

* A read opens with a marker block: *"This episode is brought to you by X."*
* It then **continues across following unlabelled blocks**, often rolling
  straight into a *second* sponsor without repeating the opening marker.
* It ends when the interview resumes — usually an explicitly labelled speaker
  turn, or an unlabelled block with no commercial signal at all.
* Reads appear both in the intro and mid-episode.

So a single regex over one block is not enough: detection has to start at the
marker and then *extend* across adjacent commercial blocks.

**Deliberately conservative.** A false positive deletes real podcast content,
which is worse than leaving one ad in. Extension therefore requires positive
commercial evidence in each block and stops at any explicitly labelled turn,
because the guest never reads ads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.parse import TranscriptBlock
from app.observability import get_logger

log = get_logger(__name__)

# --- Signals -----------------------------------------------------------------

#: Unambiguous openers. One of these is enough on its own.
#:
#: Note what is *not* here: "after a short word from our sponsors". That phrase
#: ends a genuine host intro block and merely announces the break — treating it
#: as an ad marker deleted real episode content in testing (Brian Chesky, the
#: "In our conversation, Brian shares…" block). The ad blocks that follow it
#: always carry their own "brought to you by", so nothing is lost by omitting it.
_STRONG_MARKERS = re.compile(
    r"(this episode is brought to you by"
    r"|today'?s episode is brought to you by"
    r"|this episode is sponsored by"
    r"|brought to you by our sponsor"
    r"|thank you to our sponsors)",
    re.IGNORECASE,
)

#: Weaker commercial signals. Individually they appear in genuine conversation
#: ("we ran a free trial"), so they only count toward *extending* a read that a
#: strong marker already started, and two are required.
_WEAK_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[\w-]+\.com/lenny\b", re.IGNORECASE),          # tracking URL
    re.compile(r"\blennys?podcast\b", re.IGNORECASE),
    re.compile(r"\b(promo|discount|coupon)\s+code\b", re.IGNORECASE),
    re.compile(r"\bfree trial\b", re.IGNORECASE),
    re.compile(r"\btry (it )?(for )?free\b", re.IGNORECASE),
    re.compile(r"\bsign up (today|now|at)\b", re.IGNORECASE),
    re.compile(r"\bvisit\s+[\w-]+\.(com|io|ai|co)\b", re.IGNORECASE),
    re.compile(r"\bgo to\s+[\w-]+\.(com|io|ai|co)\b", re.IGNORECASE),
    re.compile(r"\blisteners? of this podcast\b", re.IGNORECASE),
    re.compile(r"\bintroducing\s+[A-Z][\w ]{2,40}\b"),            # "Introducing Jira…"
    re.compile(r"\b\d+%\s+off\b", re.IGNORECASE),
    re.compile(r"\bspecial (offer|deal|limited)\b", re.IGNORECASE),
    re.compile(r"\bexclusive offer\b", re.IGNORECASE),
)

#: A sponsor tracking link. ``<brand>.com/lenny`` exists only because a sponsor
#: bought a Lenny-specific landing page — it does not occur in organic
#: conversation. Strong enough to open a read on its own, which is what catches
#: ad blocks whose "brought to you by" opener sits in an earlier segment.
_TRACKING_URL = re.compile(r"\b[\w-]+\s?\.?\s?com/lenny\b", re.IGNORECASE)

#: The host reads the ads. A turn labelled with the host's name can therefore
#: still be advertising; a turn labelled with the *guest's* name never is.
_HOST_NAMES = ("lenny", "lenny rachitsky")

#: Anonymous ASR labels ("Speaker 1"). Not the guest — several episodes
#: attribute the ad read to one of these.
_ANONYMOUS_SPEAKER = re.compile(r"^speaker\s*\d+$", re.IGNORECASE)

#: How far into a block a resume phrase must appear to count as "the interview
#: is starting again". Ad reads routinely *end* with "Now, back to our
#: conversation" — checking the whole block made an ad veto its own removal.
_RESUME_LOOKAHEAD_CHARS = 150

#: Phrases that mark the interview resuming. Presence vetoes ad classification.
_RESUME_MARKERS = re.compile(
    r"(back to (our conversation|the show|the episode)"
    r"|welcome to the podcast"
    r"|thank you so much for (being here|joining)"
    r"|let'?s (jump|dive) (in|back))",
    re.IGNORECASE,
)


@dataclass(slots=True)
class SponsorReport:
    """What was removed, for logging and for the ``episodes`` audit columns."""

    segments_removed: int = 0
    words_removed: int = 0
    blocks_removed: int = 0
    #: Sponsor names seen, for spot-checking the detector against an episode.
    brands: list[str] | None = None


def _resumes_interview(text: str) -> bool:
    """Does this block *begin* by returning to the conversation?

    Scoped to the opening of the block on purpose: a read that closes with
    "Now, back to our conversation" is still a read, and matching that trailing
    phrase caused ad blocks to veto their own removal.
    """
    return bool(_RESUME_MARKERS.search(text[:_RESUME_LOOKAHEAD_CHARS]))


def _weak_signal_count(text: str) -> int:
    return sum(1 for pattern in _WEAK_SIGNALS if pattern.search(text))


def _extract_brand(text: str) -> str | None:
    """Pull the sponsor's name out of an opening marker.

    Stops at the first sentence boundary. Without that, "brought to you by
    Eppo. Eppo is a next generation…" yields the brand ``"Eppo. Eppo"``, which
    then fails to match the plain word "Eppo" in the following block — so the
    rest of the read is not recognised as a continuation and leaks into the
    index. That was a real miss, not a hypothetical one.
    """
    match = re.search(r"brought to you by\s+(.{1,60})", text, re.IGNORECASE)
    if not match:
        return None
    # Truncate at sentence/clause end, then keep the leading proper-noun run.
    candidate = re.split(r"[.,!?;:]", match.group(1))[0].strip()
    tokens = candidate.split()
    brand_tokens: list[str] = []
    for token in tokens[:3]:
        if token[:1].isupper() or not brand_tokens:
            brand_tokens.append(token)
        else:
            break
    brand = " ".join(brand_tokens).strip(" .,'\"")
    return brand or None


def _is_guest_turn(block: TranscriptBlock, guest: str | None) -> bool:
    """A turn explicitly attributed to someone who is not the host.

    This is the hard stop for ad detection. The guest is never reading
    advertising, so their labelled turn always ends a read — and must never be
    removed, however commercial it sounds (guests do plug their own books,
    courses and websites, and that is genuine episode content).
    """
    if not block.is_labelled or not block.speaker:
        return False
    speaker = block.speaker.strip().lower()
    if any(speaker.startswith(host) for host in _HOST_NAMES):
        return False
    if _ANONYMOUS_SPEAKER.match(speaker):
        return False
    if guest and speaker.startswith(guest.strip().lower()[:12]):
        return True
    # An unrecognised label is treated as a guest: the conservative choice,
    # since the cost of keeping an ad is far lower than deleting real content.
    return True


def is_sponsor_start(block: TranscriptBlock, guest: str | None = None) -> bool:
    """Does this block open a sponsor read?

    Either an explicit marker, or a sponsor tracking URL. The latter matters
    because reads frequently span several blocks and a mid-read block can be
    the first one this function sees after a chain break.
    """
    # An explicit "this episode is brought to you by" is decisive, whatever the
    # transcript attributes the line to. No guest says that sentence, and some
    # episodes label the read with an anonymous or mis-attributed speaker.
    if _STRONG_MARKERS.search(block.text):
        return True
    if _is_guest_turn(block, guest):
        return False
    if _resumes_interview(block.text):
        return False
    # Standalone commercial block: a tracking URL, or an unusually dense
    # concentration of CTA language.
    return bool(_TRACKING_URL.search(block.text)) or _weak_signal_count(block.text) >= 3


def _continues_sponsor(
    block: TranscriptBlock, brand: str | None, guest: str | None = None
) -> bool:
    """Is this block a continuation of the read that just started?

    Requires the sponsor's brand plus one commercial signal, or two independent
    signals. A guest turn always ends the read; a *host* turn does not, because
    the host is the one reading the ad — in several episodes the whole read is
    labelled ``Lenny:``.
    """
    if _STRONG_MARKERS.search(block.text):
        return True
    if _is_guest_turn(block, guest):
        return False
    if _resumes_interview(block.text):
        return False

    signals = _weak_signal_count(block.text)
    if brand and re.search(rf"\b{re.escape(brand)}\b", block.text, re.IGNORECASE):
        return signals >= 1
    return signals >= 2


#: If detection wants to remove more than this share of an episode, something
#: is wrong with the heuristic — not with the episode. Ads run 1-5% of a
#: transcript in this corpus; nothing legitimate approaches 40%. This rail
#: exists because an unhandled transcript format once caused 100% of an episode
#: to be classified as advertising. Keeping the episode with an ad in it is
#: strictly better than silently losing it.
MAX_REMOVAL_FRACTION = 0.40


def strip_sponsor_blocks(
    blocks: list[TranscriptBlock],
    *,
    guest: str | None = None,
) -> tuple[list[TranscriptBlock], SponsorReport]:
    """Return the blocks worth indexing, plus a report of what was dropped.

    ``guest`` lets the detector tell the host (who reads the ads) apart from the
    guest (who never does), which is the difference between catching a read and
    deleting the interview.

    Runs a small state machine: find a strong marker, then walk forward while
    blocks still look commercial. Bridges a single ambiguous block when the one
    after it is clearly still an ad, which is what catches back-to-back reads
    where the second sponsor's opening sentence carries no marker of its own.
    """
    kept: list[TranscriptBlock] = []
    report = SponsorReport(brands=[])
    index = 0
    total = len(blocks)

    while index < total:
        block = blocks[index]

        if not is_sponsor_start(block, guest):
            kept.append(block)
            index += 1
            continue

        # --- an ad read starts here ---
        brand = _extract_brand(block.text)
        if brand:
            report.brands.append(brand)

        removed_words = len(block.text.split())
        removed_blocks = 1
        cursor = index + 1

        while cursor < total:
            candidate = blocks[cursor]
            if _continues_sponsor(candidate, brand, guest):
                if (new_brand := _extract_brand(candidate.text)) and new_brand != brand:
                    brand = new_brand
                    report.brands.append(new_brand)
                removed_words += len(candidate.text.split())
                removed_blocks += 1
                cursor += 1
                continue

            # Bridge exactly one ambiguous block if the next one is clearly
            # still commercial — the back-to-back-sponsor case.
            #
            # The bridged block carries no commercial signal of its own: a
            # second sponsor's read often opens with pure soft copy ("You fell
            # in love with building products for a reason…") and only names the
            # product in the following block. Requiring a signal here let the
            # entire Jira read through in testing. The guards below — unlabelled,
            # no resume phrase, and a *strongly* commercial successor — are what
            # keep this from eating genuine conversation.
            after = blocks[cursor + 1] if cursor + 1 < total else None
            bridgeable = (
                after is not None
                and not _is_guest_turn(candidate, guest)
                and not _resumes_interview(candidate.text)
                and (
                    _STRONG_MARKERS.search(after.text) is not None
                    or _weak_signal_count(after.text) >= 2
                )
                and _continues_sponsor(after, _extract_brand(after.text), guest)
            )
            if bridgeable:
                removed_words += len(candidate.text.split())
                removed_blocks += 1
                cursor += 1
                continue
            break

        report.segments_removed += 1
        report.blocks_removed += removed_blocks
        report.words_removed += removed_words
        index = cursor

    total_words = sum(len(block.text.split()) for block in blocks)
    if total_words and report.words_removed / total_words > MAX_REMOVAL_FRACTION:
        log.warning(
            "ingestion.sponsor_removal_aborted",
            reason="removal exceeded safety threshold; keeping the full episode",
            would_remove_words=report.words_removed,
            total_words=total_words,
            fraction=round(report.words_removed / total_words, 3),
            threshold=MAX_REMOVAL_FRACTION,
            brands=report.brands,
        )
        return list(blocks), SponsorReport(brands=[])

    return kept, report
