# Manual UI test plan

Covers what automated tests cannot: visual correctness, interaction feel,
rendering, and failure behaviour as a user experiences it.

**Time:** ~25 minutes for the full pass, ~8 minutes for the smoke path (§1–§5).

**Setup:** stack running, at least 8 episodes ingested, `MODEL_PROVIDER=ollama`.

Record results as ✅ / ❌ with notes. A ❌ needs the `X-Request-ID` from the
browser network tab so the server-side trace can be found.

---

## 1. Cold start — the evaluator path

The most important test in this document: can someone who has never seen this
repository run it from the README alone?

| # | Step | Expected |
|---|---|---|
| 1.1 | Follow README "Quick start" exactly, on a clean checkout | Stack comes up with no undocumented step |
| 1.2 | `docker compose run --rm ingest --episodes 8` | Progress logged per episode; finishes with a summary table |
| 1.3 | Open the app | Loads with no console errors |
| 1.4 | `curl localhost:8000/health/deep` | `status: ok`; database, knowledge_base and provider:ollama all `ok` |
| 1.5 | Time from clone to first grounded answer | Under 15 minutes |

---

## 2. Empty state and first answer

| # | Step | Expected |
|---|---|---|
| 2.1 | Open with no sessions | "What would you like to know?" plus four labelled suggestions. No empty panels |
| 2.2 | Click the first suggestion | Session created; message echoes **immediately**; thinking indicator appears |
| 2.3 | Wait for the answer | Markdown renders with headings/bullets; no raw `**` or `##` visible |
| 2.4 | Inspect the Sources rail | "Sources (N)" with guest, episode title, speaker, timestamp |
| 2.5 | Click a source card | YouTube opens in a **new tab**, seeked to that second |
| 2.6 | Inspect an inline `[S1]` marker | Rendered as a small superscript chip, linked to the same source |
| 2.7 | Check the metadata row | Skill chip, provider chip (`ollama · llama3.2`), latency |
| 2.8 | Hover the skill chip | Tooltip shows the router's reason |

---

## 3. Sessions and persistence

| # | Step | Expected |
|---|---|---|
| 3.1 | Click "New chat" | Empty state returns; new entry in the sidebar |
| 3.2 | Ask something different | Answer unrelated to the first session |
| 3.3 | Switch back to session 1 | Full prior history restored in order |
| 3.4 | Confirm no bleed | Session 2's topic appears nowhere in session 1 |
| 3.5 | Reload the browser | Sessions and messages survive (Postgres, not local state) |
| 3.6 | Ask a follow-up in session 1 using a pronoun ("What about for B2B?") | Resolves against the earlier topic |
| 3.7 | Check sidebar ordering | The most recently used session is first |
| 3.8 | Check the sidebar title | Derived from the first message, not "New chat" |
| 3.9 | Delete a session (× on hover) | Removed from the list; not restored on reload |

---

## 4. Grounding and honest refusal

The behaviour most worth demonstrating.

| # | Step | Expected |
|---|---|---|
| 4.1 | Ask *"What's the best recipe for sourdough bread?"* | **Declines.** States it lacks material, says what it searched, suggests covered topics |
| 4.2 | Check the refusal styling | Amber "Not enough evidence" tag; no Sources rail; not styled as an error |
| 4.3 | Check the server log | `skill.grounded_qa.declined` with `top_similarity` below threshold |
| 4.4 | Confirm no model call | No generation latency — the refusal returns in a few seconds |
| 4.5 | Ask an on-topic question | Answers normally with sources |
| 4.6 | Verify a cited claim against the linked video | The claim is actually in the transcript at that timestamp |
| 4.7 | Ask about a sponsor ("Tell me about Sidebar's peer groups") | Does **not** return ad copy as advice — sponsor segments are not indexed |

---

## 5. Artifacts

| # | Step | Expected |
|---|---|---|
| 5.1 | *"Create an HTML one-pager summarising this conversation"* | Artifact panel opens automatically |
| 5.2 | Check the chat message | Short summary naming the artifact — **not** a wall of raw HTML |
| 5.3 | Check the rendering | Styled page renders inside the panel |
| 5.4 | Inspect the iframe in devtools | `sandbox="allow-popups allow-popups-to-escape-sandbox"` — no `allow-scripts`, no `allow-same-origin` |
| 5.5 | Switch to Source view | Sanitised HTML shown; `<meta http-equiv="Content-Security-Policy">` present |
| 5.6 | Click Copy | Button confirms "Copied"; clipboard holds the artifact |
| 5.7 | Click Download | `.html` file downloads with a slugified name |
| 5.8 | Click Regenerate | New version produced; version number increments |
| 5.9 | *"Create a markdown checklist of the tactics we discussed"* | Markdown artifact renders formatted (not raw) |
| 5.10 | Reopen the session later | Artifacts persist and are selectable |
| 5.11 | If a safety report appears | Amber panel lists what was removed, in plain language |

---

## 6. Ship 30 for 30 essay

| # | Step | Expected |
|---|---|---|
| 6.1 | *"Write a Ship 30 for 30 essay about early-stage growth tactics"* | Routes to the essay skill (chip reads "Ship 30 essay") |
| 6.2 | Wait (this is the slowest path on a local model) | Essay renders in the Artifact Viewer |
| 6.3 | Count words | 1,150–1,350 |
| 6.4 | Check the opening | Single `#` headline that names who it is for and what it promises |
| 6.5 | Check structure | ≥3 `##` sections following one consistent pattern (Steps *or* Lessons) |
| 6.6 | Check skimmability | Bullet lists present; bold used selectively, not everywhere |
| 6.7 | Check rhythm | Short single-line paragraphs used as bookends (1/3/1) |
| 6.8 | Check the ending | A "takeaway" section with one specific action |
| 6.9 | Check grounding | ≥3 distinct `[S#]` sources; guests named |
| 6.10 | Inspect `messages.metadata.quality` | Validator report present, with `failures` listed honestly if it did not pass |

---

## 7. Failure behaviour

Each of these should produce a clear, actionable message — never a blank screen,
a raw stack trace, or a spinner that never resolves.

| # | Induce | Expected |
|---|---|---|
| 7.1 | Stop Ollama (`docker compose stop ollama`), ask a question | "Cannot reach the local Ollama server" with a **Try again** button |
| 7.2 | `/health/deep` while Ollama is down | `provider:ollama` reports `down` with the reason; app still serves |
| 7.3 | Restart Ollama, retry | Succeeds |
| 7.4 | Set `OLLAMA_MODEL=nonexistent`, restart API, ask | "The model 'nonexistent' is not available" naming `ollama pull` |
| 7.5 | Stop Postgres, ask a question | "The conversation store is unreachable"; retryable |
| 7.6 | Stop the API entirely, ask | "Cannot reach the API. Is the backend running on port 8000?" |
| 7.7 | Submit an empty message | Send button disabled — cannot submit |
| 7.8 | Paste >4,000 characters and send | Validation error naming the limit |
| 7.9 | Open a deleted session's URL | 404 handled; not an empty conversation |
| 7.10 | Fresh database, no ingestion | `/health/deep` reports `knowledge_base: down` with the ingestion command |
| 7.11 | Check any error body | Contains `code`, `message`, `retryable`, `request_id`; **no connection string, no stack trace** |

---

## 8. Model switching

| # | Step | Expected |
|---|---|---|
| 8.1 | Click the model pill in the sidebar | Status dialog opens with components, providers, knowledge base |
| 8.2 | With no API key | `anthropic` shown unavailable with "ANTHROPIC_API_KEY is not set" — not a crash |
| 8.3 | Set `ANTHROPIC_API_KEY` and `MODEL_PROVIDER=anthropic`, restart API | Sidebar pill shows `anthropic`; `/api/models` agrees |
| 8.4 | Ask a question | Answered by the cloud model; metadata chip shows the provider and model |
| 8.5 | Compare | Cloud answers are faster and cite more consistently — the documented trade-off |
| 8.6 | Revert to `ollama` | Local serving resumes; embeddings were local throughout |

---

## 9. Responsive and accessibility

| # | Step | Expected |
|---|---|---|
| 9.1 | Resize to 1100px | Artifact panel narrows; layout intact |
| 9.2 | Resize to 375px (phone) | Sidebar becomes an overlay behind ☰; no horizontal scroll |
| 9.3 | Open an artifact at 375px | Artifact takes the full screen; toggle returns to chat |
| 9.4 | Tab through the whole page | Every control reachable; focus ring always visible |
| 9.5 | `Enter` in the composer | Sends |
| 9.6 | `Shift+Enter` | New line, does not send |
| 9.7 | `Escape` in the status dialog | Closes |
| 9.8 | Toggle the theme | Both themes readable; no unstyled flash |
| 9.9 | Set OS to dark, reload with no override | Follows the system preference |
| 9.10 | Enable reduced motion | Pulse and sidebar transition disabled |
| 9.11 | Screen reader on the thinking state | Announced politely |
| 9.12 | Zoom to 200% | Usable; no clipped text |

---

## 10. Sign-off

| Area | Result | Notes |
|---|---|---|
| Cold start (§1) | | |
| First answer (§2) | | |
| Sessions (§3) | | |
| Grounding & refusal (§4) | | |
| Artifacts (§5) | | |
| Ship 30 essay (§6) | | |
| Failure behaviour (§7) | | |
| Model switching (§8) | | |
| Responsive & a11y (§9) | | |
