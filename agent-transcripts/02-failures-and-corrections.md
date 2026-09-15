# Failures and corrections

Eight real defects from the build. Each entry records how it surfaced, why it
happened, and what changed. All are covered by regression tests.

---

## 1. Sponsor detection deleted genuine podcast content

**Symptom.** Running the ingestion pipeline over three real episodes and diffing
kept-vs-removed blocks showed this block removed from the Brian Chesky episode:

> *"In our conversation, Brian shares an in-depth explanation of what's happening
> with product management at Airbnb… after a short word from our sponsors."*

That is Lenny's genuine episode introduction.

**Cause.** `"after a short word from our sponsors"` was in the list of strong ad
markers. It is not an ad — it is the sentence that *ends* a real intro and
announces the break. The actual ad arrives in the *next* block with its own
"brought to you by".

**Why it mattered.** False positives here are worse than false negatives. An
indexed ad is a nuisance; deleted episode content is silent data loss that
nobody notices until an answer is missing its best source.

**Fix.** Removed the phrase from the markers entirely; the following ad blocks
carry their own unambiguous marker, so nothing is lost.

**Test.** `test_keeps_genuine_intro_containing_sponsor_transition`

---

## 2. Brand extraction crossed a sentence boundary

**Symptom.** Reported sponsor brands looked wrong: `'Eppo. Eppo'`, `'Sidebar. Are'`,
`'Coda. Coda'`. Ad text was still leaking into chunks.

**Cause.** The regex captured up to three capitalised tokens after "brought to
you by" without stopping at punctuation. `"brought to you by Eppo. Eppo is a
next generation…"` yielded the brand `"Eppo. Eppo"`. Continuation detection then
searched the *next* block for the literal string `"Eppo. Eppo"`, did not find it,
and concluded the ad had ended — so the rest of the read was indexed.

**Fix.** Truncate at the first sentence/clause boundary, then take the leading
proper-noun run.

**Test.** Covered via `test_removes_ad_read_and_its_continuation`, which asserts
the brand is exactly `"Sidebar"`.

---

## 3. An entire episode classified as advertising

**Symptom.** Validating across ten more episodes:

```
adriel-frederick   words=13687 removed=13687 (100.0%)   <-- HIGH
```

100% of the episode removed.

**Cause.** The corpus has **two transcript formats**. Most episodes use
`Speaker (00:12:34):`. Roughly a third use bare `Speaker:` with no timestamps at
all. The parser only knew the first form, found zero blocks, and fell back to
"treat the whole body as one block". That single block contained one ad marker —
so the whole episode was removed.

**Why it mattered.** This was invisible from the code. Three episodes had been
tested and all passed. Only widening the sample exposed it, and only because the
removal *percentage* was being printed rather than just a pass/fail.

**Fix, in two parts:**

1. Added a parser pattern for the timestamp-free format, consulted only when the
   timestamped patterns find nothing (so a stray `Note:` line inside a normal
   episode can never be mistaken for a speaker label).
2. Added a **safety rail**: if detection wants to remove more than 40% of an
   episode, abort, log `ingestion.sponsor_removal_aborted`, and keep the episode
   intact. Ads run 1–5% of a transcript; nothing legitimate approaches 40%.

The rail is the more important half. The parser bug is fixed, but the rail means
the *next* unknown format degrades to "an ad got indexed" rather than "an
episode vanished".

**Tests.** `test_parses_timestamp_free_format`,
`test_safety_rail_aborts_excessive_removal`

---

## 4. Ad reads vetoed their own removal

**Symptom.** After fixing 1–3, five chunks still contained ad text across two
episodes.

**Cause.** Two compounding issues.

*(a)* Resume markers were matched anywhere in a block. Ad reads routinely *end*
with "Now, back to our conversation" — so the block's own closing sentence
vetoed its removal.

*(b)* The rule "a labelled speaker turn is never an ad" was too strong. The
**host** reads the ads; in several episodes the entire read is labelled
`Lenny:` or attributed to an anonymous `Speaker 1`.

**Fix.**

- Resume markers count only within the first 150 characters of a block.
- Distinguish host from guest: a *guest*-labelled turn is never an ad (and must
  never be removed — guests legitimately plug their own books and sites); a
  *host*-labelled turn can be.
- An explicit "this episode is brought to you by" overrides the speaker check
  entirely, since no guest says that sentence.

**Result.** Zero ad leakage across all 13 sampled episodes; mean removal 3.3%,
max 4.9%.

**Tests.** `test_removes_host_labelled_ad_read`, `test_never_removes_a_guest_turn`

---

## 5. Lexical search returned zero results for every question

**Symptom.** The first real end-to-end query declined to answer. The log line:

```
retrieval.search  dense_hits=40  lexical_hits=0  top_similarity=...
```

`lexical_hits=0` against a corpus that plainly discusses the topic.

**Cause.** `websearch_to_tsquery` **conjoins** every term. The question
*"What did guests say about finding product-market fit?"* became:

```
'guest' & 'say' & 'find' & 'product-market' & 'fit'
```

Requiring all five words in a single ~320-word passage matched **zero rows**.
Half of the hybrid retrieval was silently dead.

**Fix.** `build_or_tsquery()` extracts content terms, drops question
scaffolding, escapes tsquery operators, and joins with `|`. `ts_rank_cd`
restores precision by ranking passages matching more terms higher. Same query
went from 0 to 40 hits.

**Why it survived review.** The code looked correct and the function name reads
like it does the right thing. Only the log line exposed it.

**Test.** `test_builds_a_disjunctive_query`

---

## 6. The grounding threshold could never have worked

**Symptom.** The assistant declined an on-topic question with
`top_score: 0.0164` against a threshold of `0.030`.

**Cause.** The threshold was applied to the **fused RRF score**. RRF encodes
*rank*, not relevance: `score = Σ 1/(60 + rank)`. A document ranked first by one
arm scores `1/61 = 0.0164` — whether it is a perfect match or nonsense. The
maximum with both arms agreeing is `0.0328`. So a threshold of `0.030` demanded
near-perfect agreement between arms, and any dense-only match declined
automatically.

More fundamentally: **thresholding a rank-based score cannot measure
confidence.** The dense arm always returns its top-k, so the score answers "did
retrieval return anything", which is always yes.

**Fix.** Gate on two absolute signals instead:

- **Cosine similarity** of the best dense hit — an absolute measure.
- **A conjunctive lexical match** — some passage containing *every* content term.

Then **calibrate against the real corpus** rather than guessing:

| Query | Similarity |
|---|---|
| "What did guests say about finding product-market fit?" | 0.704 |
| "How should early stage startups think about growth?" | 0.720 |
| "How do you hire a great product manager?" | 0.646 |
| "Best recipe for sourdough bread with rye starter" | 0.450 |
| "Explain quantum chromodynamics gauge field equations" | 0.421 |
| "Who won the 1994 FIFA World Cup final?" | 0.426 |

Clean separation. Threshold set to **0.55**, in the gap.

**A second trap avoided.** The initial fix also accepted "any lexical hit" as
evidence. But the disjunctive query returns ~40 hits for *everything* —
"sourdough bread" matches on the words "best" and "starter" alone. Only the
*conjunctive* match is usable as a confidence signal. That is now documented in
the code and locked by a test.

**Tests.** `TestGroundingDecision`, especially
`test_disjunctive_hit_count_is_not_a_confidence_signal`

---

## 7. Follow-up questions lost the conversation topic

**Symptom.** In a live session:

```
Q1: "What did guests say about finding product-market fit?"  -> good answer
Q2: "What about for B2B specifically?"                        -> LinkedIn ads
```

**Cause.** Generation received the conversation history, but **retrieval only
ever saw the latest message**. Searching for "B2B specifically" returned
passages about B2B marketing channels — the product-market-fit topic was gone
before the model was ever consulted. The answer was grounded, just in the wrong
thing, which is worse than not answering.

**Fix.** `build_retrieval_query()` detects follow-ups (explicit openers, or
short questions with dangling references) and splices the previous user
question's content terms into the *search* text only. Generation still receives
the real history.

**Trade-off taken deliberately.** The textbook fix is an LLM query rewrite,
which is more accurate. It also costs a full extra generation round trip —
30–90 seconds on the CPU-bound local model that is the mandated demo path —
before retrieval can begin. The heuristic costs microseconds, is deterministic,
and is testable. Documented in the module so the next engineer knows it was a
choice, not an oversight, and knows when to reverse it.

**Tests.** `TestFollowUpQueryRewriting`

---

## 8. The local model answered correctly and cited nothing

**Symptom.** A well-grounded answer naming the right guests and reflecting real
transcript content — with `citations_used: 0`. No `[S#]` markers at all.

**Cause.** llama3.2 (3B) followed the *content* instruction and dropped the
*formatting* one. The prompt asked for citations in a bulleted list among other
rules; the model complied with everything except the markup.

**Fix, in two parts:**

1. **Prompt.** Made citation the most prominent section, with a worked
   right-vs-wrong example and a closing three-point checklist. Small models
   weight the end of a prompt heavily.
2. **Contract.** Sources returned to the UI are now the full evidence set the
   answer was *generated from*, not only what the model remembered to mark up.
   Inline markers remain the stronger per-claim signal — still validated, still
   stripped when fabricated — and both counts are reported separately
   (`citations_used_inline`, `citation_compliance`) so compliance stays
   measurable per model rather than being quietly papered over.

**Result:** inline citation compliance went from **0/5 to 4/5** on the same
question, with zero fabrications.

**Why the second half matters.** Returning an empty source list would have
hidden real evidence from the user. Returning the evidence set *and* reporting
that the model only marked up 80% of it is the honest version.

---

## What this list says about the process

Six of these eight were invisible from reading code. They were found by:

- Printing **percentages, not booleans** (the 100% removal)
- Reading **structured log fields** (`lexical_hits: 0`)
- **Doing the arithmetic** on a threshold (RRF max is 0.0328; the threshold was 0.030)
- **Widening the sample** from 3 episodes to 13
- **Running the actual product** and reading the answer (the missing citations,
  the lost follow-up topic)

The build moved quickly because it was AI-assisted. It is *correct* because each
claim was checked against real output before being believed. When an agent and a
reviewer both find code plausible, only execution settles it.
