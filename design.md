# design.md — UI/UX decisions

The interface for an assistant whose whole value is *being checkable*. Every
decision below follows from that.

---

## 1. Principles

**1. Provenance is the product, not a footnote.**
Most AI chat UIs treat sources as an afterthought — a collapsed accordion, a
superscript nobody clicks. Here the source cards sit directly beneath the
answer, at full width, showing guest, episode and timestamp. Clicking one opens
the episode *at the second the passage is spoken*. The interface's job is to
make verification take one click, because an unverifiable answer is worth less
than the podcast itself.

**2. Show the machine's reasoning where it changes trust.**
Each answer carries which skill handled it, which model produced it, and how
long it took. A refusal is tagged "Not enough evidence" rather than being
dressed up as an answer. A sanitised artifact says what was removed. None of
this is debug output — it is what lets someone decide whether to rely on the
answer.

**3. Deliberate hierarchy.**
The answer is the largest, highest-contrast element. Metadata is 11px and
tertiary. Sources sit between them. A reader's eye should land on the answer
first, every time, without a conscious decision.

**4. One accent colour, spent carefully.**
A single indigo marks the primary action, the active session, and citation
markers. Everything else earns attention through weight and spacing. Colour
that means something specific stops meaning anything when it is everywhere.

**5. Every state is designed.**
Empty, loading, answered, declined, error-with-retry, sanitised, offline. A
product that only looks right on the happy path is a demo, not a product.

### Anti-patterns avoided

The brief links [Impeccable](https://impeccable.style/), whose premise is
turning "AI slop into interfaces you're proud to ship". It names the patterns
that make generated UIs feel generic. Each was avoided deliberately:

| Anti-pattern | What we did instead |
|---|---|
| **AI beige** — the default slate/zinc gradient look | A committed neutral palette with one purposeful accent, and a real dark theme rather than an inverted light one |
| **Status-chip soup** — a chip on every property | Chips only where the value changes a decision: skill, provider, refusal. Latency and timestamps are plain text |
| **Everything equal** — uniform weight and size | 22px/16.5px/14px/11.5px type scale with three text colours; the answer dominates by design |
| **Cards in cards** — nested shadowed boxes | Panels are separated by one hairline border. Source cards are the only card, and they are never nested |
| **Vague headline** — "Welcome to your AI assistant" | The empty state asks "What would you like to know?" and offers four *specific, runnable* prompts |
| **Generic CTA** — "Submit", "Get Started" | Actions name their outcome: "New chat", "Regenerate", "Open artifact" |
| **Side-tab border** — decorative rails | Structural borders only, where a real boundary exists |

---

## 2. Information architecture

```
┌──────────┬────────────────────────────────┬──────────────────────┐
│ Sidebar  │ Workspace                      │ Artifact Viewer      │
│ 260px    │ fluid                          │ 460px (conditional)  │
│          │                                │                      │
│ brand    │ ── topbar ──────────────────── │ title · type · v     │
│ New chat │  session title      theme  ▥   │ ──────────────────── │
│          │                                │ Preview │ Source     │
│ Today    │ ── chat ────────────────────── │ Copy Download Regen  │
│  ·session│   user bubble (right)          │ ──────────────────── │
│  ·session│                                │                      │
│ Yesterday│   assistant answer             │  sandboxed iframe    │
│  ·session│   Sources (5) ▸ cards          │  or rendered MD      │
│          │   skill · provider · 12.4s     │                      │
│ ──────── │                                │  safety report       │
│ ● Model: │ ── composer ────────────────── │  (when applicable)   │
│   ollama │  [ textarea            ] [↑]   │                      │
└──────────┴────────────────────────────────┴──────────────────────┘
```

**Three panes, one job each.** Navigation, conversation, output. The artifact
panel appears only when there is an artifact, so the chat gets the full width
until an artifact earns the space — and the layout answers "where did my
document go?" before it is asked.

**Sidebar grouping.** Sessions bucket into Today / Yesterday / This week /
Earlier. A flat reverse-chronological list stops being scannable after about a
dozen rows, and finding a past conversation is this panel's entire purpose.

**Status in the sidebar footer.** A coloured dot and the active model, always
visible. It answers "is this running locally?" without leaving the page, and
opens the full component breakdown on click.

---

## 3. Key interaction states

### Empty state
A direct question — "What would you like to know?" — one sentence explaining
the grounding guarantee, and four suggestion cards labelled by *kind*
(Grounded answer, Ship 30 essay, HTML artifact). They teach the product's
range in the place where a user is most likely to type nothing.

### Thinking
The user's message appears **immediately** (optimistic echo), followed by
"Searching transcripts and composing an answer…" with three pulsing dots. On a
CPU-only local model an answer can take 30–120 seconds; a UI that shows nothing
for a minute reads as broken. The copy names the actual stages so the wait feels
accounted for. Animation is disabled under `prefers-reduced-motion`.

### Answered
Markdown-rendered answer → Sources rail → metadata row. Inline `[S1]` markers
render as small superscript chips linked to the same source as the card below,
so a claim and its evidence are connected in both directions.

### Declined — the state most products hide
When evidence is insufficient the assistant says so, states what it searched,
and suggests what the archive does cover. The turn is tagged **"Not enough
evidence"** in amber. It is styled as a legitimate outcome, not an error,
because it is one — and making it visually distinct is what stops a refusal
being mistaken for a failure.

### Error
A bordered alert with a plain-language title ("The model took too long"), the
specific message, the error code and request id in monospace for support, and a
**Try again** button *only when the error is retryable*. The optimistic message
is rolled back and the text restored to the composer, so nothing is lost.

### Artifact
Opens automatically beside the chat. Preview/Source toggle, Copy, Download,
Regenerate. If sanitisation removed anything, an amber panel lists what and why
before the content — the user learns the rendered artifact is not byte-identical
to what was generated.

---

## 4. Responsive behaviour

| Width | Layout |
|---|---|
| ≥ 1180px | Three columns: 260 / fluid / 460 |
| 960–1180px | Artifact panel narrows to 380px |
| < 960px | Sidebar becomes an overlay with a scrim, opened by ☰. The artifact panel goes **full screen** when open |

On a phone a 380px artifact beside a 380px chat makes both unusable, so the
artifact takes the whole screen and the topbar toggle returns to chat. The chat
column is capped at 760px on wide screens — long-form answers need a readable
measure, not the full width of a 27-inch display.

---

## 5. Accessibility

- **Semantic structure** — `<nav>`, `<header>`, `<aside>`, real `<button>`s.
  Nothing interactive is a bare `<div>` without `role` and keyboard handlers.
- **Keyboard** — everything reachable by Tab. `Enter` sends, `Shift+Enter`
  newlines (both shown in the composer hint). `Escape` closes the status dialog.
- **Visible focus** — a 2px accent outline with offset on every interactive
  element, via `:focus-visible` so it does not fire on mouse clicks.
- **Live regions** — the thinking indicator is `role="status"` `aria-live="polite"`;
  errors are `role="alert"`.
- **Labels** — icon-only buttons carry `aria-label`. The session list uses
  `aria-current`. View toggles use `aria-pressed`.
- **Contrast** — body text `#16181d` on `#ffffff` (~16.1:1); secondary `#5b6270`
  (~6.4:1); the lightest tertiary `#868d9b` is reserved for non-essential
  metadata and still clears 4.5:1 on panel backgrounds.
- **Motion** — `prefers-reduced-motion` disables the pulse and the sidebar
  transition.
- **Theme** — light and dark both defined explicitly, following the system
  preference with a manual override that wins in both directions.

---

## 6. Design decisions worth defending

**Sources as cards, not footnotes.** Cards cost vertical space that could hold
more answer. They earn it: they carry guest, episode and timestamp — the three
things needed to decide whether to trust a claim — and they make the deep link
a large, obvious target rather than a superscript.

**Latency shown, not hidden.** Displaying "12.4s" invites the thought "that's
slow". It is slow, on a 3B CPU model. Showing it sets an honest expectation and
makes the provider switch feel like a real lever rather than a setting.

**The artifact panel is conditional.** A permanent empty panel would waste a
third of the screen and require explaining. Appearing on first artifact makes
its relationship to the conversation self-evident.

**Optimistic echo with rollback.** Showing the user's message instantly is worth
the complexity of removing it on failure — the alternative is a UI that appears
to swallow input for a minute.

**Preview *and* Source for artifacts.** Preview is the point. Source exists
because a developer evaluating an HTML artifact wants to read the markup, and
because seeing the sanitised output is how you confirm the security claim
rather than taking it on trust.

**A theme toggle in a demo app.** Two lines of state, and it demonstrates the
palette is a real token system rather than hardcoded greys. The reference
designs showed both themes; matching that was deliberate.
