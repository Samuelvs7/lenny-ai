# Architecture

How The Lenny Growth Assistant is put together, why it is shaped this way, and
what a client engineer needs to know to run, debug and extend it.

---

## 1. System overview

```
┌──────────────────────────────────────────────────────────────────────┐
│ Browser — React 18 + TypeScript (Vite)                               │
│                                                                      │
│  Sidebar          │ Chat + Sources        │ Artifact Viewer          │
│  sessions, status │ answers, citations    │ sandboxed <iframe>       │
└─────────┬────────────────────────────────────────────┬───────────────┘
          │ REST (JSON)                                │ srcDoc, no scripts
┌─────────▼────────────────────────────────────────────▼───────────────┐
│ FastAPI                                                              │
│  · pydantic request/response contracts                               │
│  · structured errors  { code, message, retryable, request_id }       │
│  · X-Request-ID correlation on every log line                        │
│  · /health (liveness)  /health/deep (per-component readiness)        │
└─────────┬────────────────────────────────────────────────────────────┘
          │
┌─────────▼────────────────────────────────────────────────────────────┐
│ Agent layer                                                          │
│                                                                      │
│   Router ─ rules first, model fallback, grounded-QA default          │
│      │                                                               │
│      ├── GroundedQA     answer strictly from retrieved passages      │
│      ├── Ship30Essay    ~1,250 words + programmatic quality gate     │
│      └── ArtifactGen    Markdown | HTML → sanitiser → Artifact       │
└─────────┬──────────────────────────────┬─────────────────────────────┘
          │                              │
┌─────────▼──────────────┐   ┌───────────▼─────────────────────────────┐
│ Retrieval              │   │ Providers (LLMProvider protocol)        │
│  lexical: Postgres FTS │   │  ├─ OllamaProvider     (local, default) │
│  dense:  NumPy matrix  │   │  └─ AnthropicProvider  (cloud, opt-in)  │
│  fusion: RRF (k=60)    │   │  FallbackLLMProvider wraps a pair       │
└─────────┬──────────────┘   └───────────┬─────────────────────────────┘
          │                              │
┌─────────▼──────────────────────────────▼─────────────────────────────┐
│ Citation validator — every [S#] must resolve to a retrieved passage  │
└─────────┬────────────────────────────────────────────────────────────┘
          │
┌─────────▼────────────────────────────────────────────────────────────┐
│ PostgreSQL 14+ (no extensions required)                              │
│  sessions · messages · artifacts · episodes · chunks · ingestion_runs │
└──────────────────────────────────────────────────────────────────────┘
```

### Component boundaries

| Layer | Owns | Must not |
|---|---|---|
| `api/` | HTTP contracts, validation, error rendering | Contain business logic |
| `agent/` | Routing, turn orchestration, persistence of a turn | Know about HTTP |
| `skills/` | One capability each: prompt, execution, output validation | Know about HTTP or the database |
| `retrieval/` | Search, ranking, citation construction and validation | Call a generation model |
| `providers/` | Talking to model backends; mapping their errors to ours | Know about skills or sessions |
| `ingestion/` | Corpus → clean, chunked, embedded rows | Be on the request path |
| `db/` | Schema, queries, driver-error translation | Leak SQLAlchemy types upward |

Skills are pure functions of `(SkillContext, RetrievalResult) → SkillResult`.
That is what makes them testable without a database, a model, or a network.

---

## 2. Database schema

Plain SQL in `backend/app/db/migrations/*.sql`, applied idempotently at API
startup. **No extensions are required** — not even pgvector — so the same
schema runs on Docker Postgres, Supabase, Railway, or a bare local server.

### Conversation state

```sql
sessions
  id             UUID PK
  title          TEXT            -- derived from the first user message
  user_id        TEXT            -- 'local-user'; no auth in this build
  user_metadata  JSONB
  model_provider TEXT            -- provider at creation time
  model_name     TEXT
  created_at, updated_at, archived_at  TIMESTAMPTZ
  INDEX (user_id, updated_at DESC) WHERE archived_at IS NULL

messages
  id             UUID PK
  session_id     UUID → sessions ON DELETE CASCADE
  role           TEXT CHECK (user|assistant|system)
  content        TEXT
  skill          TEXT            -- which skill produced this turn
  router_reason  TEXT            -- why the router chose it
  model_provider, model_name TEXT
  latency_ms     INTEGER
  citations      JSONB           -- validated citations only
  metadata       JSONB           -- retrieval scores, refusal reason, tokens
  created_at     TIMESTAMPTZ
  INDEX (session_id, created_at ASC)

artifacts
  id, session_id → sessions, message_id → messages
  kind           TEXT CHECK (markdown|html)
  title, content TEXT
  version        INTEGER         -- per (session, title); regenerate keeps history
  safety_report  JSONB           -- what the sanitiser removed, and the policy
  created_at     TIMESTAMPTZ
```

`messages.skill` and `router_reason` are persisted, not just logged, so routing
behaviour is auditable after the fact. `citations` stores only validated
entries, which means the database cannot contain a fabricated source.

### Knowledge base

```sql
episodes
  id, slug UNIQUE            -- 'brian-chesky'; natural key for idempotency
  guest, title, youtube_url, video_id, publish_date, description
  duration_seconds, view_count, channel, keywords TEXT[]
  source_url, content_hash   -- hash lets re-ingestion skip unchanged episodes
  sponsor_segments_removed, sponsor_words_removed   -- data-quality audit
  ingested_at

chunks
  id             BIGINT IDENTITY PK
  episode_id     UUID → episodes ON DELETE CASCADE
  chunk_index    INTEGER
  content        TEXT
  speaker        TEXT
  start_seconds  INTEGER      -- powers the citation deep link
  end_seconds, word_count
  tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
  embedding      BYTEA        -- float32 little-endian
  embedding_dim, embedding_model
  UNIQUE (episode_id, chunk_index)
  INDEX USING GIN (tsv)

ingestion_runs
  id, started_at, finished_at, status CHECK (running|succeeded|failed)
  source_ref, embedding_model
  episodes_seen/ingested/skipped, chunks_written, sponsor_segments_removed, error
```

`tsv` is a generated column: it can never drift from `content`, because no
application code is responsible for refreshing it.

### Why explicit SQL rather than ORM models

The schema is declared once, in SQL. Mirroring it in Python ORM classes creates
two declarations of the same tables that drift apart, and the SQL file is what a
client DBA will actually read. Application code uses the async SQLAlchemy engine
with explicit statements, and repositories map rows to Pydantic DTOs. The cost
is no automatic migration generation; the benefit is one source of truth.

---

## 3. API endpoints

Base URL `http://localhost:8000`. Interactive docs at `/docs`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. Shallow and dependency-free. |
| `GET` | `/health/deep` | Readiness. Per-component status with actionable detail. |
| `GET` | `/api/models` | Providers, availability, active one, agent runner. |
| `GET` | `/api/knowledge-base` | Index size, embedding model, sponsor-removal counts. |
| `POST` | `/api/sessions` | Create a session → `201`. |
| `GET` | `/api/sessions` | List sessions for the sidebar. |
| `GET` | `/api/sessions/{id}` | Session with full message history and artifacts. |
| `PATCH` | `/api/sessions/{id}` | Rename → `204`. |
| `DELETE` | `/api/sessions/{id}` | Delete; messages and artifacts cascade → `204`. |
| `POST` | `/api/sessions/{id}/chat` | Send a message; returns answer, route, citations, artifact. |
| `GET` | `/api/sessions/{id}/artifacts` | Artifacts for a session. |
| `GET` | `/api/artifacts/{id}` | One artifact. |

### Error contract

Every failure returns the same shape:

```json
{
  "error": {
    "code": "provider_unavailable",
    "message": "The selected model is not available right now.",
    "retryable": true,
    "request_id": "8b06e0e3bcc94e77",
    "detail": "Connection refused at http://localhost:11434. Is `ollama serve` running?"
  }
}
```

`message` is written for the user; `detail` is for the developer and is omitted
when `APP_ENV=production`. `retryable` drives whether the UI offers a retry
button. Codes: `invalid_request`, `not_found`, `database_unavailable`,
`provider_unavailable`, `provider_timeout`, `provider_bad_response`,
`retrieval_unavailable`, `skill_failed`, `artifact_generation_failed`,
`artifact_unsafe`, `internal_error`.

**Liveness vs readiness.** `/health` never touches a dependency: an orchestrator
restarting the API because Postgres blinked turns a recoverable dependency
failure into an outage. `/health/deep` always returns `200` with a per-component
breakdown, because "Ollama is up but `llama3.2` is not pulled" is the answer an
operator needs and a bare `503` does not carry it.

---

## 4. Ingestion pipeline

```
GitHub (ChatPRD/lennys-podcast-transcripts)
  │  list episodes/ · fetch transcript.md (cached on disk)
  ▼
parse      YAML frontmatter + speaker/timestamp blocks
  │        two formats: "Speaker (00:12:34):" and bare "Speaker:"
  ▼
clean      strip sponsor ad-reads (see §5)
  ▼
chunk      ~320 words, 60-word overlap, split on speaker turns
  │        carries speaker + start_seconds forward
  ▼
embed      Ollama nomic-embed-text (768-d), batched
  ▼
store      episodes + chunks, one transaction per episode
```

**Operational properties**

- **Idempotent** — episodes keyed by slug with a content hash; unchanged
  episodes are skipped. Safe to schedule.
- **Resumable** — one transaction per episode. A failure at episode 40 leaves
  39 usable.
- **Degrades** — if embeddings are unavailable, chunks are still stored and
  lexical retrieval still works. The run reports success-without-embeddings
  rather than failing, because a lexical-only assistant beats none.
- **Auditable** — every run writes an `ingestion_runs` row.

**Refreshing:** re-run `python -m app.ingestion`. Only changed episodes are
re-processed. Add `--force` to rebuild everything (needed after changing chunk
size or the embedding model). Restart the API afterwards, or the in-memory dense
index will still hold the previous vectors.

---

## 5. Data quality: sponsor removal

The source transcripts inline host-read advertisements with the interview:

> *"This episode is brought to you by Sidebar… visit sidebar.com/lenny"*

Indexed naively, the assistant answers growth questions by quoting ad copy. The
detector (`ingestion/sponsors.py`) runs a small state machine: find an explicit
marker, then extend across adjacent blocks that still show commercial signals,
stopping at the guest's next turn.

Four behaviours exist because of bugs found against the real corpus:

1. *"after a short word from our sponsors"* is **not** an ad marker — it ends a
   genuine host intro. Treating it as one deleted real content.
2. Brand extraction stops at the sentence boundary. `"brought to you by Eppo.
   Eppo is a…"` otherwise yields the brand `"Eppo. Eppo"`, which then fails to
   match `"Eppo"` in the next block, and the rest of the read leaks in.
3. The **host** reads the ads, so a `Lenny:`-labelled turn can be advertising; a
   *guest*-labelled turn never is, and is never removed however commercial it
   sounds (guests plug their own books legitimately).
4. Resume phrases are only honoured in the first 150 characters of a block —
   reads routinely *end* with "now, back to our conversation", and matching that
   let an ad veto its own removal.

**Safety rail:** if detection wants to remove more than 40% of an episode it
aborts, logs `ingestion.sponsor_removal_aborted`, and keeps the episode intact.
This exists because an unhandled transcript format once caused 100% of an
episode to be classified as advertising. An indexed ad is a nuisance; a silently
deleted episode is data loss.

Measured across 13 episodes: mean 3.3% removed, max 4.9%, zero ad text leaking
into chunks.

---

## 6. Retrieval

### Hybrid, fused with RRF

Two arms, because each covers the other's blind spot:

- **Lexical** — Postgres full-text search over the generated `tsv` column.
  Nails rare terms: "PMF", "ICP", "Superhuman", a guest's name.
- **Dense** — cosine similarity over `nomic-embed-text` embeddings. Catches
  paraphrase: *"how do I know when I've found it"* → a passage that never says
  "product-market fit".

Fused with **Reciprocal Rank Fusion**, `score(d) = Σ 1/(k + rank_i(d))`, `k=60`.
RRF is used because the two arms produce incomparable scores — `ts_rank_cd` is
unbounded, cosine is `[-1, 1]` — and normalising them requires constants that
drift with the corpus. RRF fuses on rank alone.

**Lexical search uses OR, not AND.** `websearch_to_tsquery` and
`plainto_tsquery` both conjoin terms, so *"What did guests say about finding
product-market fit?"* becomes `guest & say & find & product-market & fit` and
matched **zero rows** against a corpus full of PMF discussion. That silently
disabled half of retrieval until it showed up as `lexical_hits: 0` in the logs.
`build_or_tsquery()` extracts content terms, drops question scaffolding, escapes
tsquery operators, and joins with `|`; `ts_rank_cd` restores precision.

### The grounding gate

Deciding whether to answer at all is **not** a threshold on the fused score.
RRF encodes rank, not relevance: the dense arm always returns its top-k, so its
best hit scores `1/61 = 0.0164` whether the passage is perfect or nonsense.
Thresholding that measures "did retrieval return anything", which is always yes.

The gate uses two absolute signals:

- **Cosine similarity** of the best dense hit. Calibrated against this corpus,
  not guessed: on-topic product/growth queries score **0.65–0.72**, clearly
  off-topic queries **0.42–0.45**. The default `RETRIEVAL_MIN_SIMILARITY=0.55`
  sits in that gap.
- **A conjunctive lexical match** — some single passage containing *every*
  content term. Independent evidence the archive covers the topic.

Note the disjunctive `lexical_count` is **not** usable as confidence: "sourdough
bread" matches 40 passages on the words "best" and "starter" alone.

Below the gate, the skill declines **without calling the model at all**. Asking
a model to answer from no evidence and hoping it admits ignorance is how RAG
systems hallucinate; not asking is the only reliable defence, and it is faster.

### Why not pgvector

pgvector is not in stock PostgreSQL. Requiring it means the app only runs where
someone remembered to install an extension, and adds a failure mode to every
deployment target. Instead vectors live in Postgres as `BYTEA` and load into a
single NumPy matrix at startup; search is one matrix-vector product.

**Where this stops working**, stated plainly: memory is `chunks × dims × 4`
bytes — the full 269-episode archive is ~30k chunks × 768 dims ≈ **92 MB**, with
query times in single-digit milliseconds. Beyond ~100k chunks, or the moment you
run more than one API replica (each holds its own copy and reloads on deploy),
move to pgvector with an HNSW index. The change is contained to
`retrieval/index.py`: implement `search` against SQL and delete the file.
Nothing above the retrieval package changes.

### Citations

`Citation` objects can only be constructed from chunks retrieval actually
returned this turn. `validate_citations()` strips any `[S#]` the model invents,
counts the fabrications, and logs them. A fabricated source therefore cannot
reach the UI or the database — grounding is structural, not a prompt promise.

Each citation carries a **deep link**: `youtube_url` + `&t={start_seconds}s`,
so clicking a source opens the episode at the moment the passage is spoken.
That is what makes grounding checkable in one click rather than merely claimed.

Sources returned to the UI are the full evidence set the answer was generated
from; inline `[S#]` markers are the stronger per-claim signal layered on top.
Both counts are reported (`citations_used_inline`, `citation_compliance`), so
citation discipline stays measurable per model — small local models reliably
follow the content instruction and drop the formatting one.

---

## 7. Agent routing

```
question
  │
  ├─ rules (deterministic, fast, free)
  │    "ship 30" / "write an essay"       → Ship30Essay
  │    "html" / "web page" / "one-pager"  → ArtifactGen(html)
  │    "create/make a doc|checklist|brief"→ ArtifactGen(markdown)
  │    starts with what/how/why … or "?"  → GroundedQA
  │
  ├─ model classification (only if rules are ambiguous)
  │
  └─ default → GroundedQA
```

Rules run first because these intents are lexically distinctive, a classifier
round-trip costs latency before any work starts, and a 3B model silently
mis-routing a Ship 30 request to plain Q&A is the worst failure mode.

Grounded Q&A is the default because it is the safe outcome: answering when an
artifact was wanted is a minor annoyance; generating an artifact when an answer
was wanted discards the user's question.

Routing never fails a request — if the classifier errors or returns prose, the
default applies. Every decision logs `skill`, `method` and `matched`, and both
the reason and method are persisted on the message.

### Skills

| Skill | Retrieval | Output | Validation |
|---|---|---|---|
| `GroundedQA` | top-5 | answer + citations | grounding gate, citation validation |
| `Ship30Essay` | top-14 | ~1,250-word Markdown + artifact | length band, headings, bullets, bold, takeaway, ≥3 sources, 1/3/1 rhythm; one bounded revision pass |
| `ArtifactGen` | top-10 | Markdown or HTML artifact + chat summary | sanitisation, safety report |

The Ship 30 skill encodes the frameworks from the published guide — Curiosity
Gap, the five headline pieces, the 4A paths, credibility types, Wheels & Spokes,
the 1/3/1 rhythm, Rate of Revelation — in a versioned prompt file, and checks
the output programmatically. Prompts live in `skills/prompts/*.md` rather than
as string literals, so they can be reviewed as prose and diffed meaningfully.

---

## 8. Model abstraction

```python
class LLMProvider(ABC):
    name: str
    @property
    def model(self) -> str: ...
    async def generate(messages, *, max_tokens, temperature, stop) -> LLMResponse
    async def health() -> ProviderHealth        # must never raise
```

Everything above depends only on this protocol, so switching providers is a
configuration change:

```bash
MODEL_PROVIDER=ollama      # local, no key, the mandated demo path
MODEL_PROVIDER=anthropic   # cloud, needs ANTHROPIC_API_KEY
```

Generation and embeddings are **separate protocols** on purpose: the right
pairing is usually mixed — generation on a cloud or local chat model, embeddings
local and free. Forcing one provider to do both would make that impossible.

`FallbackLLMProvider` wraps a pair. Only *infrastructure* failures fall through
(unavailable, timeout, bad response); a refusal or malformed request is not
retried, because the second attempt fails identically and the user waits twice
as long to find out. The response records which provider actually served it.

**Failure handling.** Every provider maps its transport errors onto our types,
so a missing key, a stopped Ollama, an unpulled model and a timeout each produce
a distinct, actionable message. A provider that cannot be constructed is
recorded as unavailable with the reason rather than raising at import — the app
must start when the cloud provider is unconfigured, because local-only is the
supported default.

### Agent runner: a documented tension

The brief requires the Claude Agent SDK **and** mandates that the demo run on
Ollama. The SDK does not drive Ollama. Rather than fake either requirement, the
skills and their contracts are defined once and `AGENT_RUNNER` selects the
executor: `native` (provider-agnostic, powers the local demo, the default) or
`claude_agent_sdk` (cloud, requires an API key and the Claude Code CLI). The
native runner is what is verified end to end here; see `agent-transcripts/` for
the full reasoning and what remains unverified.

---

## 9. Security

Detailed in [`docs/security.md`](docs/security.md), including residual risks.
Summary:

| Surface | Control |
|---|---|
| Generated HTML | Allow-list sanitisation (`nh3`/ammonia) → injected CSP `default-src 'none'` → iframe `sandbox` **without** `allow-scripts` or `allow-same-origin`. |
| Generated Markdown | Server-side stripping of script/handler constructs, plus DOMPurify at render time. |
| Prompt injection | Retrieved passages are delimited and explicitly framed as untrusted data; the prompt instructs the model to treat embedded commands as quoted speech. |
| Secrets | Read from the environment only. `.env` is gitignored; `describe_safe()` reduces keys to booleans; DSNs are stripped of credentials before logging. |
| Error responses | `detail` suppressed in production; connection strings never rendered. Covered by a test. |
| SQL injection | Parameterised statements throughout; free-text search terms are stripped to alphanumerics before reaching `to_tsquery`. |
| Container | Non-root user; no secrets baked into images. |

We do not claim the artifact viewer is "secure". We claim it is layered,
explicit about what it permits and blocks, and honest about what remains.

---

## 10. Observability

Structured logs via `structlog` — human-readable locally (`LOG_FORMAT=console`),
one JSON object per event in production (`LOG_FORMAT=json`).

Every request carries an `X-Request-ID` (generated, or echoed from the caller)
that appears on every log line for that request and in every error body. Grep
one id to see the full trace: routing decision, retrieval counts and scores,
model call and latency, database operations, artifact sanitisation.

Key events: `http.request`, `router.decision`, `retrieval.search`,
`agent.skill_run`, `agent.turn_complete`, `skill.grounded_qa.declined`,
`grounding.fabricated_citations`, `artifact.sanitised`, `provider.fallback_engaged`,
`ingestion.episode_done`, `db.migration.apply`.

Latency is recorded per stage, because "it's slow" is the most common complaint
about a RAG system and it is unactionable without knowing which stage was slow.

We log shapes and counts — token estimates, chunk counts, scores, durations —
never prompt or response bodies, and never secrets.

---

## 11. Deployment topology

```
docker compose up --build -d
docker compose run --rm ingest --episodes 8

  web   :8080   nginx → static React bundle
  api   :8000   uvicorn → FastAPI (migrations run at startup)
  ollama:11434  model runtime      (volume: ollama-models)
  postgres:5432 database           (volume: postgres-data)
  ingest        one-shot job, profile "tools"
```

The API depends on Postgres being *healthy*, not merely started, which is what
stops it racing the database on a cold `up`. Migrations run automatically at
startup and are idempotent, so there is no separate migration step. Model pulls
and the transcript cache live in named volumes so a restart does not re-download
2 GB.

**Scaling beyond one node.** The dense index is per-process and rebuilt at
startup, so multiple API replicas each hold their own copy. That is fine to a
few replicas and a corpus of this size; past that, move the index to pgvector
(§6) and the API becomes stateless.

---

## 12. Extending the system

**Add a skill:** implement `Skill` in `skills/`, add its prompt to
`skills/prompts/`, register it in `Agent._skills`, add a rule or classifier
label in `agent/router.py`, add the enum value to `SkillName`, and write a
routing test.

**Add a model provider:** implement `LLMProvider`, map its exceptions to the
`ProviderError` family, register it in `ProviderRegistry._build()`, add the enum
value to `ProviderName`. Nothing else changes.

**Change chunking or the embedding model:** update `.env`, then re-ingest with
`--force`. The two must match between ingestion and query time; the API logs
`retrieval.mixed_embedding_models` and `retrieval.query_dimension_mismatch`
when they do not.

**Add authentication:** `sessions.user_id` and `user_metadata` already exist and
are threaded through the repositories. Populate `user_id` from your auth
middleware and scope `list_summaries` to it — no schema migration required.
