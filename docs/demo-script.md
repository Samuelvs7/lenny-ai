# Demo script — 2 to 3 minutes

Camera on. The goal is to show the product working and communicate one real
engineering decision — not to narrate every feature.

**Before recording**

- Stack running, ≥ 8 episodes ingested, `MODEL_PROVIDER=ollama`
- One session pre-warmed so the model is loaded (a cold first call adds ~20s)
- Browser at 1440×900, dark or light — pick one and stay there
- A second window with `docker compose logs -f api` ready for the Ollama beat
- **Rehearse once.** Local generation takes 30–120s per answer; know where the
  waits are and talk through them rather than sitting in silence

---

## 0:00–0:20 — The problem

> "A growth PM listens to Lenny's Podcast at 1.5× on her commute. She remembers
> that someone said something useful about product-market fit — she has no idea
> who, or which episode. Generic AI will answer that question confidently and
> unattributably, which is useless when she has to defend a decision in a
> meeting.
>
> This is The Lenny Growth Assistant. Every answer is grounded in the actual
> transcripts, and every claim links to the second it was said."

---

## 0:20–1:00 — Grounded answer with verifiable citations

Open the app. Click the first suggestion, or type:

> *"What did guests say about finding product-market fit?"*

While it generates:

> "It's searching the transcripts with hybrid retrieval — Postgres full-text
> search for exact terms, plus embeddings for paraphrase — then fusing the two.
> This is a 3-billion-parameter model on CPU, so it takes about a minute."

When the answer lands, **click a source card**.

> "That's the important part. It opened the episode at 39 minutes 7 seconds —
> exactly where that claim was made. You can check it in one click. Every
> citation is validated against what retrieval actually returned, so the model
> cannot invent a source; fabricated markers get stripped before you see them."

---

## 1:00–1:20 — Follow-up, then the refusal

Type:

> *"What about for B2B specifically?"*

> "Session context carries. And note the search query is rewritten behind the
> scenes — 'what about for B2B' on its own would have searched for 'B2B' and
> lost the topic."

Then, in the same session or a new one:

> *"What's the best recipe for sourdough bread?"*

> "This is the behaviour I care most about. It doesn't guess. Retrieval scored
> 0.45 against a 0.55 threshold, so it declines and tells you what it searched —
> and it never called the model at all. Most RAG demos will happily answer this."

---

## 1:20–1:50 — Artifact generation and the viewer

> *"Create an HTML one-pager summarising this conversation"*

> "Generated HTML is untrusted. It's sanitised server-side against an
> allow-list, given a Content-Security-Policy of `default-src none`, and
> rendered in an iframe **without** `allow-scripts` and **without**
> `allow-same-origin` — so it can't run script, reach the network, or touch the
> parent page. If anything was stripped, the panel tells you what."

Toggle to **Source** briefly, then back to Preview.

---

## 1:50–2:10 — Running locally on Ollama

Click the model pill in the sidebar.

> "Everything you just saw ran on `llama3.2` locally through Ollama — no API key,
> no data leaving the machine. Embeddings are local too. Switching to a cloud
> model is one environment variable; the application code doesn't change,
> because everything depends on a provider interface rather than a vendor SDK."

Show the status dialog: components green, 30 episodes, 2,327 passages,
58 sponsor segments removed.

---

## 2:10–2:40 — One trade-off, honestly

Pick **one**. The sponsor story is the most distinctive; the pgvector one is the
most technical. Do not do both.

**Option A — the data-quality find (recommended):**

> "The thing I'd flag from this engagement: the source transcripts have the
> host-read ads inlined with the interview. If you index them naively, the
> assistant cites a Sidebar ad as growth advice. So ingestion strips them —
> about 3% of each episode, 58 segments across the corpus.
>
> It's a heuristic, so it has a safety rail: if detection ever wants to remove
> more than 40% of an episode it aborts and keeps everything. I added that after
> an unhandled transcript format caused it to delete an entire episode. The
> failure mode is now 'an ad got indexed', never 'an episode vanished'."

**Option B — the retrieval trade-off:**

> "I deliberately didn't use pgvector. Vectors live in Postgres and load into a
> NumPy matrix at startup — 92 MB for the full archive, single-digit millisecond
> queries. That means this runs on any stock Postgres, including Supabase or a
> bare local server, with no extension to install. It breaks past about 100,000
> chunks or a second API replica, and the migration path is one file. For v1,
> 'a fresh evaluator can run it' beat 'scales to a million documents'."

---

## 2:40–3:00 — Close

> "Everything is documented for handoff: the PRD with assumptions and what I
> deliberately left out, an architecture doc with the schema and trade-offs, a
> security doc that's honest about residual risk, 129 tests, and a Docker
> Compose setup that comes up in one command.
>
> The top backlog item is streaming — on a local model it's the single biggest
> perceived-quality win, and I cut it to get correctness right first."

---

## Recording notes

- **Do not** wait in silence for generation. Talk through what's happening.
- If a generation stalls past ~2 minutes, cut and restart — do not narrate an
  outage.
- Have the refusal example ready; it is the most differentiated moment.
- Click a source card on camera. Watching the video jump to the timestamp is
  more convincing than any description.
- Mention `llama3.2:1b` if latency looks bad on the day — it is ~3× faster.
