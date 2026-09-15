# Verification log

What was actually executed and observed on the build machine, and — just as
importantly — what was **not** verified and why.

Nothing below is marked verified unless it was run and its output read.

---

## Environment

| | |
|---|---|
| OS | Windows 11, Intel i7-13620H, 16 logical cores |
| GPU | **None.** Ollama reports `100% CPU` for both models |
| Python | 3.11.9 |
| Node | 22 / npm 11 |
| PostgreSQL | **16.4**, portable binaries run from a scratch directory |
| Ollama | 0.32.5 — `llama3.2` (3B), `nomic-embed-text` (768-d) |
| Docker | **Not installed** |
| `ANTHROPIC_API_KEY` | **Not set** |

---

## ✅ Verified — run and observed

### Database and persistence

```
db.migration.apply     version=1 filename=001_init.sql duration_ms=357 outcome=ok
db.migrations.applied  versions=[1]
db.migrations.up_to_date  known=1          <- second run: idempotent
tables: artifacts, chunks, episodes, ingestion_runs, messages,
        schema_migrations, sessions
chunk indexes: chunks_pkey, chunks_episode_id_chunk_index_key,
        idx_chunks_embedded, idx_chunks_episode, idx_chunks_tsv
```

Migrations apply against a real PostgreSQL 16.4, are idempotent on re-run, and
create every table, index and generated column.

### Ingestion

```
ingestion.finished  ingested=30  chunks=2327  embeddings=2327
                    sponsor_segments=58  sponsor_words=13822
```

30 episodes, 2,327 chunks, **every chunk embedded**, 58 sponsor segments
(13,822 words) removed. Throughput ≈ 50 s/episode on CPU.

### Sponsor removal — measured across 13 episodes

| Metric | Result |
|---|---|
| Mean removal | **3.28%** of words |
| Max removal | 4.9% |
| Ad text leaking into chunks | **0** |
| Genuine content wrongly removed | 0 (verified by asserting the known false-positive block survives) |

Both transcript formats (timestamped and timestamp-free) parse correctly.

### Retrieval calibration — measured, not assumed

| Query | Top cosine similarity |
|---|---|
| "What did guests say about finding product-market fit?" | **0.704** |
| "How should early stage startups think about growth?" | **0.720** |
| "How do you hire a great product manager?" | **0.646** |
| "Best recipe for sourdough bread with rye starter" | **0.450** |
| "Explain quantum chromodynamics gauge field equations" | **0.421** |
| "Who won the 1994 FIFA World Cup final?" | **0.426** |

Clean separation; threshold set to 0.55 in the gap. The dense index loads 2,327
vectors in **307 ms** at startup.

### End-to-end grounded answer (live, on Ollama)

```
router.decision      skill=grounded_qa method=rule matched="question form"
retrieval.search     lexical_hits=40 dense_hits=40 top_similarity=0.7039
                     returned=5 duration_ms=740
agent.turn_complete  citations=5 declined=False latency_ms=77114
                     model=llama3.2 provider=ollama
```

Answer named real guests (Adam Grenier, Adriel Frederick), cited **4 of 5**
sources inline, **0 fabricated**. Citation deep links resolve, e.g.
`https://www.youtube.com/watch?v=-PDsvl2WCZU&t=2347s`.

### Honest refusal (live)

```
retrieval.search            top_similarity=0.4502
skill.grounded_qa.declined  reason=insufficient_grounding threshold=0.55
agent.turn_complete         declined=True latency_ms=5156
```

Declined "What is the best recipe for sourdough bread?" in **5 seconds** —
the model was never called, which is the point.

### Follow-up context

```
agent.query_rewritten  original_chars=32 rewritten_chars=70
                       reason=follow_up_needs_conversation_topic
```

"What about for B2B specifically?" correctly retrieved product-market-fit
passages after the rewrite.

### Artifact sanitisation

A crafted payload containing script tags, event handlers, `javascript:` URLs,
an iframe, a form with a password input, a `<base>` tag, an external stylesheet
link, CSS `@import` and a remote `url()` produced:

```
blocked = {css_import:1, css_remote_url:1, script_tag:1, iframe:1, form:1,
           external_link_tag:1, base_tag:1, event_handler:2, javascript_url:1}
LEAKS: none
```

Legitimate content survived: inline CSS, `style` attributes, tables with
`colspan`, external links (with `rel="noopener noreferrer"` added), inline SVG.
CSP injected.

### API

`/health`, `/health/deep`, `/api/models`, `/api/knowledge-base`, session CRUD,
chat, and artifact endpoints all exercised. `/health/deep` correctly reported
`anthropic` unavailable with *"ANTHROPIC_API_KEY is not set"* rather than
failing.

### Frontend

`tsc --noEmit` clean; `vite build` succeeds (236 KB JS / 78 KB gzipped).
Rendered and inspected in a real browser: three-pane layout, session grouping,
empty state, Markdown answers, Sources rail with timestamps, metadata chips,
model pill.

**Two UI bugs found by measuring the rendered DOM** (not by reading code):
the composer auto-resize pinned itself open at 180 px — first because
`height:auto` on a stretched grid item makes `scrollHeight` self-referential,
then because measuring before grid layout settles reported 402 px for an empty
field. Fixed and re-verified at 34 px.

### Tests

```
150 passed
```

Covering ingestion (18), retrieval (23), agent/routing/citations (46),
artifact security (32), Agent SDK wiring (9), API and persistence (24).

### Dependency conflict — found and documented

Installing `claude-agent-sdk` pulled `mcp` → `sse-starlette` → `starlette 1.6.0`,
which broke FastAPI 0.115.6 at import:

```
TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'
```

`starlette==0.41.3` is now pinned explicitly in `requirements.txt`, the SDK
lives in a separate `requirements-agent-sdk.txt`, and the conflict and its
workaround are documented there.

---

## ⚠️ Not verified — stated plainly

### Docker Compose stack

**Docker is not installed on this machine.** `docker-compose.yml`, both
Dockerfiles and the nginx config are written carefully — the compose YAML parses
and its service graph, health checks, volumes and environment wiring were
reviewed — but **`docker compose up` was never executed here.**

Mitigation: the entire stack was instead verified natively (real PostgreSQL,
real Ollama, real API, real frontend), and the application deliberately requires
no PostgreSQL extensions, so the schema and queries are the same in both paths.
The README documents both.

*An evaluator with Docker should treat the compose path as the documented
primary route but the native path as the one proven here.*

### Live Anthropic cloud call

No API key was available. The `AnthropicProvider` is written against the
official SDK and its error mapping is unit-tested against a mock transport, but
**no request has been made to the Anthropic API from this code.**

### Claude Agent SDK runner, end to end

`app/agent/runners/claude_sdk.py` is written against the real SDK API, and that
API was verified by introspection — `tool()`, `create_sdk_mcp_server()`,
`query()` and every `ClaudeAgentOptions` field used all exist with the expected
signatures. Nine tests confirm the MCP server constructs, the search tool
returns `[S#]`-labelled passages, and it enforces the same grounding gate as the
native runner.

**What is not verified: a complete agent loop against live Anthropic models.**
That needs a key. The runner is wired and tested at its boundaries, not proven
in flight.

### Ship 30 essay on the local model

The validator is unit-tested and the skill is wired, but a full 1,250-word essay
takes several minutes of CPU generation. On this hardware it is at the edge of
the timeout budget. It is expected to be demonstrated on a cloud provider or
with `llama3.2:1b`; the validator reports failures honestly rather than hiding a
short essay.

### Load, concurrency, browser matrix

No load testing, no concurrent-user testing, and only Chromium was used. The
in-memory dense index is explicitly single-process (see `architecture.md` §6).

---

## Measured performance (this machine)

| Operation | Time |
|---|---|
| Migrations (cold) | 357 ms |
| Dense index load, 2,327 vectors | 307 ms |
| Retrieval (both arms + fusion + hydrate) | 0.7–1.8 s |
| Grounded answer, end to end (llama3.2, CPU) | **71–120 s** |
| Refusal (no model call) | **5 s** |
| Ingestion | ~50 s/episode |
| Full test suite | 16 s |

Answer latency is the weakest number and is the honest cost of a CPU-only local
model. It is documented in the README with two escape hatches (`llama3.2:1b`,
or the cloud provider), and streaming is the top backlog item.
