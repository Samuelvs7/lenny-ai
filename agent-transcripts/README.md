# Agent transcripts — how this was built

The assignment asks for coding-agent transcripts "including failed attempts and
how you corrected them". This folder is that record, written honestly.

This project was built in one session with Claude (Claude Code) directing the
implementation, with me setting direction, verifying claims, and deciding the
trade-offs. What follows is the substance: what went wrong, how it was caught,
and what changed as a result.

Secrets and machine-specific paths have been removed. No `.env` content appears
anywhere in this repository.

## Contents

| File | What it covers |
|---|---|
| [`01-discovery-and-plan.md`](01-discovery-and-plan.md) | Reading the brief, extracting the real source URLs, environment constraints found before any code |
| [`02-failures-and-corrections.md`](02-failures-and-corrections.md) | **The important one.** Eight real bugs, how each surfaced, and what changed |
| [`03-verification-log.md`](03-verification-log.md) | What was actually executed and observed, and what could not be verified here |

## The honest summary

Eight substantive defects were found and fixed during the build. Six of them
came from **running the code against the real corpus rather than trusting it**:

1. A sponsor-detection rule that deleted genuine podcast content
2. Brand extraction crossing a sentence boundary, leaking half of every ad read
3. A second transcript format in the corpus that caused **100% of an episode**
   to be classified as advertising
4. Ad reads vetoing their own removal via a trailing "back to our conversation"
5. Lexical search using AND semantics, silently returning **zero** results for
   every natural-language question
6. A grounding threshold set against fused RRF scores, which encode rank rather
   than relevance — so it could never have worked
7. A follow-up question losing the conversation topic before retrieval
8. The local model producing correct answers with **zero** inline citations

Numbers 3, 5 and 6 were invisible from the code alone. Each was caught by
looking at real output — logs showing `lexical_hits: 0`, a removal rate of
100%, a threshold that mathematically could never be exceeded by a single
retrieval arm.

The lesson worth carrying into a client engagement: **an AI-assisted build moves
fast enough that verification, not generation, becomes the bottleneck.** Every
one of these bugs was written confidently and looked reasonable on review. They
were only found by running the thing and reading what came back.
