# PRD — The Lenny Growth Assistant

**Status:** v1 shipped and running locally
**Owner:** Forward Deployed Engineer
**Engagement:** product & growth team wants Lenny's Podcast transcripts turned
into a reliable internal assistant

---

## 1. Discovery brief

### The user

**Priya, a growth PM at a 40-person Series A B2B SaaS company.** She has three
to five product decisions in flight at any time and no research function. She
listens to Lenny's Podcast because it is the densest source of operator-level
tactics she knows, but she listens at 1.5× while commuting and remembers
*roughly* what someone said, not who said it or in which episode.

Secondary users on the same team: a founder preparing a board narrative, and a
product marketer who needs to turn internal knowledge into publishable writing.

### The job to be done

> *"When I'm deciding something — how to price, when to hire a PM, whether we
> have PMF — I want to know what operators who've actually done it said, with
> enough specificity and attribution that I can bring it into a decision
> meeting without being asked 'according to whom?'"*

### The pain today

1. **Recall without retrieval.** She knows the answer exists in an episode. She
   cannot find it. Scrubbing a 90-minute video is not a research method.
2. **Generic AI is worse than useless here.** ChatGPT answers product questions
   with plausible, unattributable consensus advice. In a decision meeting,
   "an AI said so" carries no weight. Attribution *is* the value.
3. **Insight doesn't become artifacts.** Even when she finds the answer, turning
   it into something shareable — a one-pager, a post, a brief — is another hour
   she does not have.

### What the assistant removes

The gap between *"I know this was discussed somewhere"* and *"here is the claim,
the person who made it, and the timestamp where they said it."*

---

## 2. Success metrics

**Primary — grounded answer rate.**
*Share of on-topic questions answered with ≥1 valid citation and zero fabricated
citations.* **Target: ≥ 90%.**

This is the metric because it is the product. An assistant that answers
confidently without provenance is a worse tool than the podcast itself. It is
measured, not estimated: `citations_used_inline`, `citations_available` and
`citations_fabricated` are written to `messages.metadata` on every turn, so the
rate is a SQL query, and fabricated citations are structurally impossible to
persist (the validator strips them before storage).

**Secondary — honest refusal rate.**
*Share of deliberately out-of-scope questions that are declined rather than
answered.* **Target: ≥ 95%.** Measured by `declined_reason='insufficient_grounding'`.
A system that never says "I don't know" cannot be trusted when it says anything.

**Operational — time to first grounded answer on a clean machine.**
**Target: under 15 minutes** from `git clone`, including model pull and an
8-episode ingestion. This is the handoff metric: a solution another team cannot
stand up is not delivered.

---

## 3. Assumptions

Recorded because the brief was deliberately incomplete. Each is a decision that
could reasonably have gone another way.

| # | Assumption | Why | If wrong |
|---|---|---|---|
| A1 | Internal tool, trusted users, no authentication | No auth requirement in the brief; adding login costs a day and demos worse | `sessions.user_id` and `user_metadata` already exist — populate from middleware, no migration |
| A2 | A curated subset of episodes is enough for v1 | Embedding 269 episodes on CPU takes hours; 30 episodes covers the common product/growth questions | `--all` ingests everything; the only cost is time |
| A3 | Users want attribution more than fluency | They are taking this into meetings | If wrong, the citation UI is noise and should collapse by default |
| A4 | Answer latency of 30–90s is acceptable for a local model | These are research questions, not chat | Documented `llama3.2:1b` (~3× faster) and the cloud provider as the escape hatches |
| A5 | The transcript repo is the source of truth and is stable | It is the source named in the brief | `content_hash` makes re-ingestion cheap when it changes |
| A6 | Sponsor ad-reads must be removed | Verified by reading the data: they are inline and would be cited as advice | The rail is configurable (`INGEST_STRIP_SPONSORS=false`) |
| A7 | Artifacts are static HTML/CSS, not applications | The brief says "HTML/CSS snippets"; static content can be rendered with scripting disabled entirely | Interactive artifacts would need `allow-scripts` and a materially weaker security posture |

---

## 4. Scope

### In scope (built and verified)

- New chat, session history, independent per-session context, Postgres persistence
- Grounded Q&A with inline citations and timestamped deep links to YouTube
- Explicit refusal when evidence is insufficient
- Follow-up questions using session context
- Ship 30 for 30 essay skill with programmatic quality validation
- Markdown and HTML/CSS artifact generation
- In-app Artifact Viewer with preview/source, copy, download, regenerate
- Sanitisation + sandboxing of generated HTML, with the safety report surfaced
- Ollama (local) and Anthropic (cloud) providers, switchable by config
- Structured errors, structured logs with request correlation, health endpoints
- Docker Compose stack, idempotent migrations, one-shot ingestion job
- 129 automated tests; manual UI checklist

### Explicitly out of scope, and why

| Excluded | Why |
|---|---|
| **Authentication / multi-tenancy** | Internal tool for a trusted team (A1). Real auth means user management, sessions, and a permission model — days of work that demonstrate nothing about the core problem. The schema is ready for it. |
| **Streaming responses** | Would materially improve perceived latency on a slow local model, and is the first thing I would add. Cut because it complicates error handling mid-stream and the honest-refusal path, and correctness mattered more for v1. |
| **Full 269-episode index by default** | Hours of CPU embedding before an evaluator sees anything work (A2). Time-to-first-value beat coverage. |
| **Re-ranking model on retrieved chunks** | A cross-encoder would improve precision, but adds a second model to the demo path and another thing to be slow. Hybrid + RRF was measurably good enough (on-topic 0.65–0.72 vs off-topic 0.42–0.45). |
| **Conversation summarisation for long sessions** | Bounded history (10 turns) is sufficient at this session length and avoids a whole class of "the summary lost the point" bugs. |
| **Editing artifacts in the viewer** | Regenerate-with-guidance covers the need; a full editor is a different product. |
| **Analytics dashboard** | The data is in `messages.metadata` and queryable. A UI for it is premature before anyone has asked a question of it. |
| **Interactive (scripted) artifacts** | Would require `allow-scripts` in the iframe, weakening the security posture for a capability nobody asked for (A7). |

---

## 5. User flows

### Flow 1 — grounded answer (the core loop)

1. Priya opens the app; the empty state offers four concrete starting points.
2. She asks *"What did guests say about finding product-market fit?"*
3. Her message appears immediately; a "searching transcripts" state shows.
4. Hybrid retrieval runs; the grounding gate passes (similarity 0.70).
5. The answer renders with a **Sources** rail beneath it.
6. She clicks a source → the YouTube episode opens **at the second** the passage
   is spoken. She verifies the claim in one click.

**Acceptance:** answer cites ≥1 real source; every source resolves to a real
passage; deep links land at the right timestamp; the turn persists across reload.

### Flow 2 — honest refusal

1. She asks something the archive does not cover.
2. Retrieval returns passages, but the best similarity is 0.45 — below 0.55.
3. **The model is never called.** The assistant says it does not have enough,
   states what it searched, and suggests what the archive does cover.
4. The turn is tagged "Not enough evidence" in the UI.

**Acceptance:** no fabricated answer; no citations; `declined=true` persisted.

### Flow 3 — follow-up with session context

1. After Flow 1 she asks *"What about for B2B specifically?"*
2. The prior turn is in the prompt; the pronoun resolves.
3. A second session, opened in parallel, shows none of this history.

**Acceptance:** follow-up resolves against prior turns; sessions never leak into
each other (covered by an automated test).

### Flow 4 — Ship 30 essay

1. *"Write a Ship 30 for 30 essay about early-stage growth tactics."*
2. Router selects `ship30_essay`; retrieval widens to 14 passages.
3. The essay is generated against the encoded frameworks, then **validated**:
   length band, headline, ≥3 headings, bullets, selective bold, closing
   takeaway, ≥3 distinct sources, 1/3/1 rhythm.
4. If it fails, one bounded revision pass names the specific defects.
5. The essay opens in the Artifact Viewer; the quality report is in metadata.

**Acceptance:** 1,150–1,350 words; grounded; validator result recorded honestly
even when it does not pass.

### Flow 5 — artifact generation and rendering

1. *"Create an HTML one-pager summarising this conversation."*
2. Generated HTML is sanitised server-side; blocked constructs are counted.
3. It renders in a sandboxed iframe with scripting and same-origin disabled.
4. If anything was stripped, the viewer says what and why.
5. Copy, Download, Regenerate, and a Source view are available.

**Acceptance:** no script executes; legitimate CSS and links survive; the safety
report is visible, not just logged.

---

## 6. Acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| AC1 | New chat creates an independent session | `test_sessions_keep_independent_context` |
| AC2 | Conversations persist in PostgreSQL across restarts | `test_messages_persist_across_requests` |
| AC3 | Answers cite real transcript sources | `test_markers_map_to_retrieved_chunks`, manual |
| AC4 | Fabricated citations never reach the user or the database | `test_strips_fabricated_markers` |
| AC5 | Insufficient evidence produces a refusal, not an answer | `test_declines_without_evidence_and_never_calls_the_model` |
| AC6 | Follow-ups use session context | `test_history_from_the_same_session_reaches_the_model` |
| AC7 | Essays land in the 1,150–1,350 word band with required structure | `TestEssayValidator` + manual |
| AC8 | Artifacts render in-app, never as raw code in chat | Manual test plan §5 |
| AC9 | Generated HTML cannot execute script | `TestScriptRemoval` (32 security tests) |
| AC10 | Provider switches via config with no code change | `test_lists_providers_and_the_active_one` + manual |
| AC11 | Missing key / dead Ollama / dead DB fail gracefully | `TestProviderFailureSurface`, manual §7 |
| AC12 | Health endpoints report per-component status | `test_readiness_reports_each_component` |
| AC13 | Errors never leak secrets | `test_error_messages_never_leak_the_connection_string` |
| AC14 | A fresh evaluator can run it from the README alone | Manual test plan §1 |

---

## 7. Risks and trade-offs

| Risk | Severity | Mitigation | Residual |
|---|---|---|---|
| **Hallucination** | High | Evidence gate *before* the model call; citations constructible only from retrieved chunks; fabricated markers stripped and counted | A model can still mis-summarise a passage it was given. Deep links let a user check in one click — the mitigation is verifiability, not prevention. |
| **Local model quality** | High | Retrieval does the heavy lifting; the model only synthesises supplied context. Tight prompts, output validation, bounded revision | llama3.2 (3B) writes shorter, blunter answers than a frontier model and inconsistently applies inline citation markers. Measured and reported (`citation_compliance`) rather than hidden; the cloud provider is one env var away. |
| **Latency on CPU** | High | Trimmed context (5 passages, 1,100 chars each), 600-token ceiling, optimistic UI echo, long client timeout | 30–90s per answer on a CPU-only laptop. Documented, with `llama3.2:1b` and the cloud provider as escape hatches. Streaming is the real fix and is the top backlog item. |
| **Ad copy cited as advice** | High | Sponsor detection at ingestion with a 40% safety rail; measured at 3.3% mean removal, zero leakage across 13 episodes | Heuristic, not perfect. A novel ad format could slip through. The rail guarantees the failure mode is "an ad got indexed", never "an episode vanished". |
| **Prompt injection from transcripts** | Medium | Passages delimited and framed as untrusted data; explicit instruction to treat embedded commands as quoted speech | Not a hard boundary — an LLM instruction is not a sandbox. The blast radius is bounded: the model has no tools and no write access. |
| **Unsafe artifact rendering** | Medium | Allow-list sanitisation → CSP `default-src 'none'` → iframe without `allow-scripts`/`allow-same-origin` | A sanitiser bug upstream, or visually deceptive static content. Documented in `docs/security.md` — we do not claim "secure". |
| **Corpus drift** | Low | `content_hash` per episode; re-ingestion is incremental and idempotent | Nobody is scheduling it yet. `ingestion_runs` makes staleness visible. |
| **Single-node index** | Low | In-memory NumPy index, rebuilt at startup | Breaks past ~100k chunks or multiple replicas. Migration path to pgvector documented and contained to one file. |

### The trade-off I would defend first

**Hybrid retrieval on stock PostgreSQL instead of pgvector.**

pgvector is the conventional choice and is better at scale. I chose against it
because the binding constraint here is not scale — it is that *a fresh evaluator
must be able to run this*. Requiring an extension means the app only works where
someone remembered to install it, and adds a failure mode to every deployment
target. At this corpus size an in-memory NumPy index is ~92 MB and searches in
single-digit milliseconds, and the same schema runs unmodified on Docker,
Supabase, Railway, or a bare local Postgres.

The cost is real and stated: it breaks past ~100k chunks or multiple API
replicas. The migration is contained to `retrieval/index.py`. I would make the
same call again for v1 and reverse it the day we needed a second replica.

---

## 8. Implementation plan

| Phase | Delivered | Status |
|---|---|---|
| 1 | Config, structured logging, error taxonomy, health endpoints | ✅ |
| 2 | Postgres schema, idempotent migrations, repositories | ✅ |
| 3 | Ingestion: fetch → parse → sponsor-strip → chunk → embed → store | ✅ |
| 4 | Hybrid retrieval, dense index, citation validator | ✅ |
| 5 | Provider abstraction (Ollama, Anthropic) with fallback | ✅ |
| 6 | Router + three skills | ✅ |
| 7 | React frontend: chat, sessions, sources | ✅ |
| 8 | Artifact Viewer + sanitisation + sandbox | ✅ |
| 9 | Failure paths, observability | ✅ |
| 10 | 129 automated tests, manual checklist | ✅ |
| 11 | Docker Compose, Dockerfiles, documentation | ✅ |

### Backlog, in the order I would do it

1. **Streaming responses** — the single largest perceived-quality win on a local
   model. Deliberately deferred, not forgotten.
2. **Cross-encoder re-ranking** on retrieved chunks, once latency allows.
3. **Scheduled re-ingestion** with `ingestion_runs`-backed staleness alerting.
4. **Answer-quality eval set** — 30 question/expected-source pairs, so retrieval
   changes can be measured rather than eyeballed.
5. **pgvector migration**, when replicas or corpus size demand it.
