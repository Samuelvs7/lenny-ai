# The Lenny Growth Assistant

A grounded internal assistant over [Lenny's Podcast transcripts](https://github.com/ChatPRD/lennys-podcast-transcripts).
Ask product and growth questions and get answers that cite the episode — and the
exact second — they came from. Turn those answers into Ship 30 for 30-style
essays, or into Markdown and HTML artifacts rendered beside the chat.

Runs entirely locally on Ollama. No API key required.

---

## What it does

| Capability | What you get |
|---|---|
| **Grounded Q&A** | Answers built only from indexed transcripts, with `[S1]`-style inline citations that deep-link to the moment in the YouTube episode. |
| **Honest refusal** | When the archive does not support an answer, it says so and explains what it searched — instead of inventing one. |
| **Ship 30 for 30 essays** | ~1,250-word essays written against the [Ship 30 frameworks](https://www.ship30for30.com/post/how-to-start-writing-online-the-ship-30-for-30-ultimate-guide), then programmatically checked for length, structure, skimmability and grounding. |
| **Artifacts** | Markdown documents and complete HTML/CSS pages, rendered in an in-app viewer — not dumped as raw code into the chat. |
| **Model switching** | Ollama (local) or Anthropic (cloud), swapped by one environment variable. The active provider is visible in the UI. |

---

## Quick start (Docker Compose)

Prerequisites: Docker Desktop with Compose v2, ~6 GB free disk, ~8 GB RAM.

```bash
git clone <your-repo-url> lenny-ai && cd lenny-ai
cp .env.example .env
docker compose up --build -d
```

Wait for the model pull to finish (first run downloads ~2.3 GB):

```bash
docker compose logs -f ollama-pull
```

Load the knowledge base — **this step is required**, the assistant has nothing
to ground on until it runs:

```bash
docker compose run --rm ingest --episodes 8
```

Open **http://localhost:8080**.

> **Ingestion takes real time.** Embeddings are computed locally on CPU at
> roughly 50 seconds per episode. `--episodes 8` (~7 min) is enough to demo
> grounding end to end. The default of 30 (~25 min) covers most product and
> growth questions. `--all` ingests the full 269-episode archive and takes
> several hours.

Verify everything is healthy:

```bash
curl -s http://localhost:8000/health/deep | python -m json.tool
```

---

## Quick start (no Docker)

Prerequisites: Python 3.11+, Node 20+, PostgreSQL 14+, [Ollama](https://ollama.com).

```bash
# 1. Models
ollama pull llama3.2
ollama pull nomic-embed-text

# 2. Database
createdb lenny

# 3. Config
cp .env.example .env
# DATABASE_URL already points at localhost:5432/lenny

# 4. Backend
cd backend
python -m venv .venv
source .venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m app.ingestion --episodes 8     # migrations run automatically
uvicorn app.main:app --port 8000

# 5. Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**.

---

## Try it

1. **Ask a grounded question** — *"What did guests say about finding product-market fit?"*
   Note the source cards; click one to jump to that moment in the episode.
2. **Ask a follow-up** — *"What about for B2B specifically?"* Session context is preserved.
3. **Watch it refuse** — *"What's the best recipe for sourdough bread?"* It declines
   rather than guessing, and says what it searched.
4. **Generate an essay** — *"Write a Ship 30 for 30 essay about early-stage growth tactics"*
5. **Generate an artifact** — *"Create an HTML one-pager summarising this conversation"*
6. **Check the system** — click the model pill in the sidebar for live component health.

---

## Configuration

Every variable is documented inline in [`.env.example`](.env.example). The ones
that matter most:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `…@localhost:5432/lenny` | PostgreSQL connection (asyncpg). |
| `MODEL_PROVIDER` | `ollama` | `ollama` or `anthropic`. **Switch and restart — no code changes.** |
| `MODEL_FALLBACK_PROVIDER` | `none` | Provider to use if the primary fails. |
| `OLLAMA_MODEL` | `llama3.2` | Chat model. Use `llama3.2:1b` for ~3× faster, blunter answers. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embeddings. **Changing this invalidates the index — re-ingest.** |
| `ANTHROPIC_API_KEY` | *(empty)* | Optional. Absent means local-only; the app starts fine without it. |
| `RETRIEVAL_TOP_K` | `5` | Passages passed to the model as evidence. |
| `RETRIEVAL_MIN_SIMILARITY` | `0.55` | Grounding threshold. Raise for stricter refusal, lower if it declines too often. |
| `INGEST_MAX_EPISODES` | `30` | Episodes to index. `0` or `--all` for everything. |

**Never commit `.env`.** It is in `.gitignore`; only `.env.example` is tracked.

### Switching to the cloud model

```bash
# in .env
MODEL_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Restart the API. The sidebar pill and `/api/models` both reflect the change.
Embeddings stay local regardless — only generation moves.

---

## Tests

```bash
cd backend
pip install -r requirements-dev.txt

# Unit tests — no database, no model, no network
pytest

# Including database-backed API and persistence tests
createdb lenny_test
TEST_DATABASE_URL=postgresql+asyncpg://lenny:lenny@localhost:5432/lenny_test pytest
```

Database tests **skip with an explanatory message** when `TEST_DATABASE_URL` is
unset, rather than failing.

Frontend:

```bash
cd frontend && npm run typecheck && npm run build
```

A manual UI checklist lives in [`docs/manual-test-plan.md`](docs/manual-test-plan.md).

---

## Architecture at a glance

```
Browser (React/TS)  ──  chat │ sources │ Artifact Viewer (sandboxed iframe)
        │
FastAPI  ──  validation, structured errors, request-id logging, /health
        │
Agent    ──  router → one of: GroundedQA │ Ship30Essay │ ArtifactGen
        │
Retrieval ── Postgres FTS + dense vectors, fused with Reciprocal Rank Fusion
        │
Citation validator ── every [S#] must resolve to a real retrieved passage
        │
PostgreSQL ── sessions, messages, artifacts, episodes, chunks
```

Full detail, including the database schema and every API endpoint:
[`architecture.md`](architecture.md). Product reasoning: [`PRD.md`](PRD.md).
UI decisions: [`design.md`](design.md). Security posture and residual risks:
[`docs/security.md`](docs/security.md).

**Three decisions worth knowing up front:**

1. **No pgvector.** Vectors live in Postgres as `BYTEA` and load into a NumPy
   matrix at startup. The app therefore runs on *any* stock PostgreSQL —
   Docker, Supabase, Railway, or a bare local server — with no extension to
   install. Trade-off and migration path in `architecture.md`.
2. **Sponsor ad-reads are stripped at ingestion.** The source transcripts inline
   host-read advertisements. Left in, the assistant cites ad copy as product
   advice. ~3% of each episode is removed, with a safety rail that aborts if
   detection ever wants more than 40%.
3. **Generated HTML is untrusted.** Server-side allow-list sanitisation, an
   injected `Content-Security-Policy`, and a sandboxed iframe with scripts and
   same-origin both disabled.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *"The transcript knowledge base has not been loaded yet"* | Ingestion has not run. `docker compose run --rm ingest --episodes 8` |
| *"Cannot reach the local Ollama server"* | Ollama is not running. `ollama serve`, or `docker compose up -d ollama`. |
| *"The model 'llama3.2' is not available"* | Model not pulled. `ollama pull llama3.2` and check `ollama list`. |
| *"The model took too long to respond"* | CPU-only inference is slow. Raise `OLLAMA_TIMEOUT_SECONDS`, or switch to `OLLAMA_MODEL=llama3.2:1b`, or set `MODEL_PROVIDER=anthropic`. |
| Assistant refuses on-topic questions | Too few episodes indexed, or the threshold is too strict. Ingest more episodes, or lower `RETRIEVAL_MIN_SIMILARITY` to ~0.45. |
| Answers feel unrelated to the question | `OLLAMA_EMBED_MODEL` changed since ingestion, so query and index vectors disagree. Re-run ingestion. The API logs `retrieval.mixed_embedding_models` when it detects this. |
| *"The conversation store is unreachable"* | Postgres is down or `DATABASE_URL` is wrong. `docker compose ps`, then `curl localhost:8000/health/deep`. |
| Frontend loads but every call fails | API not running, or `CORS_ORIGINS` does not include the frontend origin. |

**First diagnostic step for anything else:** `curl -s localhost:8000/health/deep`.
It reports each component separately with an actionable message. Every response
carries an `X-Request-ID`; grep the API logs for it to see the full trace of a
single request.

---

## Repository layout

```
backend/app/
  api/         routes, schemas, dependency wiring
  agent/       router + orchestrator
  skills/      grounded_qa, ship30_essay, artifact_gen, artifact_safety
               prompts/  — versioned prompt files, not string literals
  retrieval/   hybrid search, dense index, citation validation
  providers/   LLMProvider abstraction: ollama, anthropic, registry
  ingestion/   fetch → parse → sponsor-strip → chunk → embed → store
  db/          engine, repositories, migrations/*.sql
frontend/src/  React app: Chat, Sidebar, ArtifactViewer, StatusPanel
docs/          security, manual test plan, demo script
agent-transcripts/  how this was built, including what went wrong
```
