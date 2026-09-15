# Requirement audit

Every explicit requirement in the assignment, where it is implemented, and
whether it was actually verified by running it.

**VERIFIED** means it was executed and its output observed on the build machine.
**NOT VERIFIED** entries state why, and what was done instead. Nothing is marked
verified on the strength of the code looking correct.

---

## 3.1 API, sessions, and persistence

| Requirement | Implementation | Verified |
|---|---|---|
| Backend built with FastAPI | `backend/app/main.py`, `api/routes.py` | ✅ Server runs; all endpoints exercised |
| Agent layer via Claude Agent SDK / Pi | `agent/runners/claude_sdk.py` (SDK) + `agent/orchestrator.py` (native) | ⚠️ SDK **partially** — API verified by introspection, MCP tool constructs and enforces grounding (9 tests). **No live agent loop** (no API key). Native runner fully verified |
| New chat / independent session context | `POST /api/sessions`; history scoped by `session_id` | ✅ `test_sessions_keep_independent_context` |
| Persist conversations, IDs, timestamps, user metadata in PostgreSQL | `migrations/001_init.sql`, `db/repositories.py` | ✅ Live Postgres 16.4; survives restart |
| Clear request/response contracts | `api/schemas.py` (pydantic) | ✅ Enforced, visible at `/docs` |
| Validation | `ChatRequest` bounds + blank check | ✅ 3 validation tests |
| Structured errors | `errors.py` → `{code, message, retryable, request_id}` | ✅ `test_error_body_has_the_documented_shape` |
| Health endpoints | `/health`, `/health/deep` | ✅ Both exercised; per-component detail |

## 3.2 Flexible LLM configuration

| Requirement | Implementation | Verified |
|---|---|---|
| Config layer to switch model without code changes | `MODEL_PROVIDER` env → `providers/registry.py` | ✅ Provider selection and reporting verified |
| Cloud LLM integrated | `providers/anthropic.py`, official SDK | ⚠️ Code-complete, error mapping unit-tested against a mock. **No live call** (no key) |
| Local LLM, mandatory for demo | `providers/ollama.py` | ✅ **Every end-to-end answer in this build ran on `llama3.2` via Ollama** |
| Selected provider visible in UI/config | Sidebar model pill, `/api/models`, status panel | ✅ Visible in browser |
| Fallback behaviour documented | `FallbackLLMProvider`; README + architecture.md | ✅ 3 fallback tests |

## 3.3 Knowledge base

| Requirement | Implementation | Verified |
|---|---|---|
| Use Lenny's Podcast transcripts | `github.com/ChatPRD/lennys-podcast-transcripts` | ✅ 30 episodes, 2,327 chunks ingested |
| Explain loading / chunking / indexing / refresh / traceability | `architecture.md` §4; `ingestion/` | ✅ Documented and run |
| Answers cite the transcript used | `retrieval/citations.py`; Sources rail | ✅ 5 sources, 4 cited inline, 0 fabricated |

## 4.1 Grounded conversational assistant

| Requirement | Implementation | Verified |
|---|---|---|
| RAG answering strictly from transcripts | `skills/grounded_qa.py` + hybrid retrieval | ✅ Live answer named real guests |
| Handles follow-up questions | `agent/query.py` rewrite + history in prompt | ✅ `agent.query_rewritten` observed live |
| Preserves session context | Bounded history per session | ✅ Test + live |
| Acknowledges unsupported questions | Evidence gate before the model call | ✅ Declined sourdough query in 5s, model never called |

## 4.2 Ship 30 for 30 content skill

| Requirement | Implementation | Verified |
|---|---|---|
| Dedicated skill, not a one-off prompt | `skills/ship30_essay.py` + `prompts/ship30_essay.md` | ✅ Separate skill, routed, validated |
| Principles read from the linked source and encoded | Guide read; Curiosity Gap, 5 headline pieces, 4A paths, credibility types, Wheels & Spokes, 1/3/1, Rate of Revelation | ✅ Encoded in the prompt; checked in `evaluate_essay()` |
| ~1,250 words | `MIN_WORDS`/`MAX_WORDS` band, enforced | ✅ Validator unit-tested |
| Strong hook, narrative progression | Headline + 4A + proven-approach rules; heading check | ✅ Validator |
| Skimmable formatting | Bullet, bold, heading counts | ✅ Validator |
| Specific useful takeaway | Closing-section check | ✅ Validator |
| Claims grounded | ≥3 distinct sources required | ✅ Validator |
| — | Full essay on the local CPU model | ⚠️ At the edge of the latency budget; expected on cloud or `llama3.2:1b` |

## 4.3 Artifact generation and viewer

| Requirement | Implementation | Verified |
|---|---|---|
| Generate Markdown documents | `skills/artifact_gen.py` | ✅ Routed and generated |
| Generate complete HTML/CSS | `prompts/artifact_html.md` | ✅ Routed (`artifact_kind=html` observed) |
| Based on the current conversation | History + retrieved passages in prompt | ✅ |
| Artifact Viewer renders beside chat | `ArtifactViewer.tsx` | ✅ Panel renders in browser |
| Not raw code / no redirect | Chat gets a summary; artifact renders in-panel | ✅ |
| **Treat generated HTML as untrusted** | Sanitiser → CSP → sandboxed iframe | ✅ 32 security tests; 10 attack vectors blocked, 0 leaks |
| Explain what is permitted/blocked and why | `docs/security.md` §2 + in-code tables | ✅ Including residual risks |

## 5. Deployment & operational readiness

| Requirement | Implementation | Verified |
|---|---|---|
| One-command startup | `docker-compose.yml`, `Makefile` | ⚠️ **Docker not installed here.** YAML parses and the service graph was reviewed; **never executed.** Native path fully verified instead |
| `.env.example` with safe defaults | 135 lines, every variable documented | ✅ App runs from it unmodified |
| Never commit secrets | `.gitignore`; secret scan run | ✅ Scan clean; `.env` untracked |
| Structured logs | `observability.py` + request-id correlation | ✅ Observed throughout |
| Visibility into model/retrieval/DB/artifact failures | Per-stage events with latency | ✅ Every diagnosis in this build used them |
| Handle missing keys | Provider reports unavailable with reason | ✅ Live: "ANTHROPIC_API_KEY is not set" |
| Handle unavailable Ollama | Typed error + actionable message | ✅ Unit-tested |
| Handle model timeouts | `ProviderTimeoutError` | ✅ **Observed live** (and drove the token-budget fix) |
| Handle empty retrieval | Refusal path | ✅ Live |
| Handle DB connection failure | `DatabaseUnavailableError`; app still serves `/health` | ✅ Startup degradation tested |
| Handoff documentation | README, architecture, PRD, design, security, test plan | ✅ |

## 6. Deliverables

| # | Deliverable | Status |
|---|---|---|
| 1 | Public GitHub repo, sensible structure, no secrets | ✅ Committed, scanned. **Push is the candidate's to do** |
| 2 | README | ✅ Architecture, prereqs, install, env, both model setups, run, tests, troubleshooting |
| 3 | PRD | ✅ User, problem, metrics, assumptions, scope + non-goals, flows, acceptance criteria, risks, plan |
| 4 | design.md | ✅ Principles, IA, states, responsive, accessibility, decisions |
| 5 | architecture.md | ✅ Schema, endpoints, boundaries, ingestion/retrieval, routing, model toggle, security, topology |
| 6 | Agent transcripts incl. failed attempts | ✅ `agent-transcripts/` — 8 real bugs with corrections |
| 7 | Tests + manual UI plan | ✅ 150 automated; `docs/manual-test-plan.md` |
| 8 | Demo video (2–3 min, camera on) | ❌ **Candidate must record.** Script at `docs/demo-script.md` |

---

## Outstanding for the candidate

1. **Record the demo video** — `docs/demo-script.md` is a shot-by-shot script.
2. **Push to a public GitHub repo** and submit the form.
3. *(Optional)* Add `ANTHROPIC_API_KEY` to `.env` to verify the cloud provider
   and the Agent SDK runner live.
4. *(Optional)* Run `docker compose up --build` on a machine with Docker to
   confirm the compose path.
