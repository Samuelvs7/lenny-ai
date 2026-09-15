# Discovery and planning

What happened before any code was written, and why the plan looked the way it
did.

---

## 1. Reading the brief properly

Two documents were supplied: the take-home assignment and the campus
recruitment notice. Both were read in full, and — importantly — the assignment's
**hyperlink targets were extracted**, not just its visible text. That produced
information the prose alone did not:

| Link text | Actual target | Why it mattered |
|---|---|---|
| "Lenny's Podcast / Newsletter transcript repository" | `github.com/ChatPRD/lennys-podcast-transcripts` | The real corpus — 269 episodes, YAML frontmatter, a specific structure to design ingestion around |
| "Ship 30 for 30 guide" | `ship30for30.com/post/how-to-start-writing-online-…` | The brief says to *read the linked source and encode its principles*. Reading it produced the actual frameworks — Curiosity Gap, the five headline pieces, 4A paths, Wheels & Spokes, 1/3/1 rhythm, Rate of Revelation |
| "Impeccable" | `impeccable.style` | A design tool whose pitch is "turn AI slop into interfaces you're proud to ship". It names the anti-patterns it fights: *AI beige, status-chip soup, everything equal, cards-in-cards, vague headline, generic CTA*. Its presence in "Helpful Resources" is a signal that a generic AI-demo UI is a scored negative |
| "Anthropic Claude Agent SDK" | `code.claude.com/docs/en/agent-sdk` | Confirmed which SDK was meant |

Both linked sources were then actually read. The Ship 30 frameworks are encoded
in `backend/app/skills/prompts/ship30_essay.md` and checked programmatically in
`ship30_essay.py`; the Impeccable anti-patterns are addressed explicitly in
`design.md`.

**Two requirements that are easy to skim past:**

1. Deliverable 6 asks for agent transcripts *"including failed attempts and how
   you corrected them"*. A sanitised, success-only log scores worse than an
   honest one — the brief is evaluating how AI work is directed and verified.
2. The brief says *"verify that a fresh evaluator can clone the repository and
   run the solution using only your documented steps."* That is the acceptance
   criterion for the README, and it shaped several architecture decisions.

---

## 2. Environment constraints, established before designing

Probing the machine first changed the plan materially:

| Finding | Consequence |
|---|---|
| **No Docker installed** | Compose files can be written and validated but **cannot be executed here**. A second, fully-verified path was needed — and is documented honestly rather than claimed |
| **No NVIDIA GPU**; Ollama reports `100% CPU` | Latency is the dominant UX constraint. Drove the token ceilings, context trimming, optimistic UI echo, and the decision against an LLM query-rewrite step |
| `llama3.2` present; no embedding model | `nomic-embed-text` pulled (274 MB, 768-d) |
| **No `ANTHROPIC_API_KEY`** | The cloud path is code-complete and unit-tested against a mocked transport, but a live cloud call is unverified. Stated plainly rather than implied |
| No local PostgreSQL | Portable Postgres 16.4 binaries installed to a scratch directory to get a **real** database to verify against, rather than SQLite or mocks |

The Docker finding is the one that mattered most. It forced an explicit choice:
claim a compose stack works, or build something whose *primary verified path*
does not need Docker at all. The second is what a client engineer actually
benefits from, and it is why the app requires no Postgres extensions.

---

## 3. Reading the data before designing the pipeline

Before writing the ingestion code, real transcripts were downloaded and read.
Three things came out of that which no amount of reasoning about the problem
would have produced:

**(a) Sponsor ad-reads are inlined with the interview.**

> *"This episode is brought to you by Sidebar. Are you looking to land your next
> big career move?…"*

Indexed naively, the assistant cites ad copy as product advice. This became a
core ingestion feature — and, later, the source of three separate bugs (see
`02-failures-and-corrections.md`).

**(b) Every turn carries a timestamp, and frontmatter carries `youtube_url`.**

```
Brian Chesky (00:39:00):
```

So a citation can be a **deep link to the exact second**:
`youtube.com/watch?v=…&t=2340s`. That turns "grounded" from a claim into
something a user verifies in one click. This is the single highest-value feature
in the product, and it came from reading the data rather than the brief.

**(c) The corpus is heterogeneous.** Not discovered until later — and it cost an
entire episode (failure #3).

---

## 4. Architecture decisions taken up front

| Decision | Reasoning |
|---|---|
| **Hybrid retrieval on stock Postgres, no pgvector** | pgvector is not in stock PostgreSQL. Requiring it means the app only runs where someone installed an extension. At this corpus size an in-memory NumPy index is ~92 MB and searches in single-digit ms. "A fresh evaluator can run this" beat "scales to a million documents" |
| **Explicit SQL, no ORM models** | The schema is declared once, in SQL. Mirroring it in ORM classes creates two declarations that drift, and the SQL file is what a client DBA reads |
| **Rules-first routing** | These intents are lexically distinctive. A classifier round-trip costs latency before any work starts, and a 3B model silently mis-routing a Ship 30 request is the worst failure mode |
| **Evidence gate before the model call** | Asking a model to answer from no evidence and hoping it admits ignorance is how RAG systems hallucinate. Not asking is the only reliable defence — and it is faster |
| **Two agent runners, one set of skills** | The brief requires the Claude Agent SDK *and* mandates an Ollama demo. The SDK does not drive Ollama. Rather than fake either, skills are defined once and `AGENT_RUNNER` selects the executor. The tension is documented rather than hidden |
| **Sandboxed iframe without `allow-scripts`** | The brief asks for HTML/CSS *snippets*. Static content can be rendered with scripting disabled entirely, which is a far stronger posture than sanitisation alone |

---

## 5. What was deliberately not built

Recorded so that absence reads as a decision rather than an omission. Full
reasoning in `PRD.md` §4.

- **Streaming responses** — the largest perceived-quality win on a slow local
  model, and the top backlog item. Cut because it complicates error handling
  mid-stream and the honest-refusal path, and correctness mattered more for v1.
- **Authentication** — internal-tool assumption; the schema is ready for it.
- **Cross-encoder re-ranking** — adds a second model to an already CPU-bound
  demo path. Hybrid + RRF measured well enough (0.65–0.72 on-topic vs 0.42–0.45
  off-topic).
- **Full 269-episode index by default** — hours of CPU embedding before anything
  works. Time-to-first-value beat coverage.

---

## 6. The working method

Phases were ordered so that a demoable product existed at every checkpoint:
config and health → schema → ingestion → retrieval → providers → agent → UI →
artifacts → tests → docs.

The rule applied throughout: **verify each claim against real output before
believing it.** Every phase ended with running something and reading what came
back — not with the code looking correct. That is what surfaced the eight
defects in `02-failures-and-corrections.md`, six of which were invisible from
code review.
