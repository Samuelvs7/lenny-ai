# Markdown artifact skill

Produce a single, well-structured Markdown document based on the conversation
and the supplied transcript passages.

## Output contract

- Output **only** Markdown. No preamble, no "here is your document", no
  wrapping code fence around the whole thing.
- Open with a single `#` title.
- Use `##` / `###` headings, bullet lists, and tables where they genuinely help.
- Bold selectively — for the one sentence per section that carries the point.

## Grounding

- Every factual claim traces to a supplied passage, cited inline as `[S1]`,
  `[S2]`, …
- Use only the markers that appear in the passages. Never invent one.
- Attribute by name and episode where the passage supports it.
- If the passages do not support something, leave it out rather than filling
  the gap from memory.

## What makes a good artifact here

Write a document someone would keep — a briefing, a checklist, a framework
summary, a comparison table. Not a transcript of the chat.

- Front-load the useful part. The first screen should carry the substance.
- Prefer concrete specifics from the passages (numbers, company names,
  tactics) over generic advice.
- Close with a short section the reader can act on.
