# Security

What this system defends against, how, and — importantly — what it does not.

Nothing here is described as "secure". Each control has a stated scope and a
stated residual risk.

---

## 1. Threat model

| # | Threat | Where it enters | Severity |
|---|---|---|---|
| T1 | XSS via generated HTML artifacts | Model output rendered in the browser | High |
| T2 | Data exfiltration from an artifact | Network request from rendered content | High |
| T3 | Prompt injection via transcript content | Third-party corpus → retrieval → prompt | Medium |
| T4 | Secret leakage | Logs, error responses, images, git | High |
| T5 | SQL injection | User query text reaching `to_tsquery` | Medium |
| T6 | Credential phishing via a convincing fake UI | Artifact rendering a fake login form | Medium |
| T7 | Resource exhaustion | Oversized generation or pathological CSS | Low |

The trust boundary: **everything the model produces, and everything the corpus
contains, is untrusted input.** The corpus is a third-party GitHub repository
and the model is a statistical process shaped by it. Neither gets the benefit of
the doubt.

---

## 2. T1/T2 — Artifact rendering

The core security feature. Three independent layers, because any one of them
can be wrong.

### Layer 1 — server-side sanitisation

`backend/app/skills/artifact_safety.py`. The document is taken apart, the body
is cleaned with `nh3` (Rust `ammonia`) against an **allow-list**, CSS is
filtered, and the document is rebuilt inside a shell we control.

An allow-list rather than a block-list: a block-list must anticipate every
attack; an allow-list only has to name what is permitted.

**Permitted:** structural HTML, text, tables, lists, links (`http`, `https`,
`mailto`), inline `style` attributes and `<style>` blocks, `data:` images,
inline SVG.

**Blocked, and why:**

| Blocked | Reason |
|---|---|
| `<script>`, `on*` attributes | Script execution — the primary XSS vector |
| `javascript:` / `vbscript:` URLs | Script execution via navigation |
| `<iframe>`, `<object>`, `<embed>` | Nested browsing contexts can load remote content |
| `<form>`, `<input>`, `<button>` | A realistic-looking form is a phishing surface (T6) |
| `<link>`, `<meta>`, `<base>` | Remote stylesheets, redirects, base-URL hijacking |
| CSS `@import`, remote `url()` | CSS can fetch remote resources (T2) |
| CSS `expression()`, `behavior:` | Legacy script execution via CSS |

The rebuilt document's `<head>` is **ours, never the model's** — that is what
guarantees the CSP is present and that no `<meta>`/`<base>`/`<link>` the model
emitted survives.

Everything removed is counted and returned in a `safety_report`, stored on the
artifact and **shown in the viewer**. A blocked construct is a security event
worth seeing, not something to hide.

### Layer 2 — Content-Security-Policy

Injected into every artifact document:

```
default-src 'none'; style-src 'unsafe-inline'; img-src data:;
font-src 'none'; script-src 'none'; connect-src 'none';
form-action 'none'; base-uri 'none'; frame-src 'none'
```

Even if markup slipped past layer 1, it cannot reach the network. `style-src
'unsafe-inline'` is required for generated CSS to work at all; since
`default-src` is `'none'`, inline CSS still cannot fetch anything remote.

### Layer 3 — iframe sandbox

`frontend/src/components/ArtifactViewer.tsx`:

```html
<iframe sandbox="allow-popups allow-popups-to-escape-sandbox"
        srcDoc={artifact.content} referrerPolicy="no-referrer" />
```

Note what is **absent**:

- **No `allow-scripts`** — scripting is disabled inside the frame entirely.
- **No `allow-same-origin`** — the document gets a unique opaque origin. It
  cannot read the parent DOM, cookies, or `localStorage`, or call the API with
  the user's context.
- **No `allow-forms`** — a fake login form cannot submit.

`allow-popups` is granted only so a legitimate link (a cited episode) can open
in a new tab.

Because scripts are disabled at the sandbox level, an XSS payload surviving
layers 1 and 2 would still not execute. That is the point of layering.

### Residual risks — stated, not solved

- **A defect in `nh3`/`ammonia` upstream** would weaken layer 1. Layers 2 and 3
  are what make that survivable.
- **Visually deceptive static content.** A convincing fake login form built from
  pure CSS can still be *rendered*. It cannot submit or execute, but a user
  could be misled. Mitigated socially: the viewer labels artifacts as generated
  content. This is the weakest point of the design.
- **CSS resource consumption** (T7). Pathological selectors or huge animations
  can burn CPU. Confined to the iframe; the user can close the panel.
- **`data:` images are permitted** for usability. Size is bounded by
  `MAX_ARTIFACT_CHARS` (200,000).

### Markdown artifacts

Markdown permits raw HTML, so the same vectors apply. Handled twice:
server-side stripping before storage (so the *database* never holds a script),
and DOMPurify with a strict allow-list at render time. The renderer does not
assume its input was cleaned, because a future caller might not clean it.

**Verification:** 32 automated tests in `tests/test_artifact_safety.py` cover
each vector above, asserting both that the attack is neutralised *and* that
legitimate content survives. A sanitiser that strips everything is safe and
useless.

---

## 3. T3 — Prompt injection from the corpus

Transcripts come from a third-party repository. A guest reading an instruction
aloud, or a malicious commit to the corpus, becomes text in our prompt.

**Controls:**

- Retrieved passages are wrapped in explicit delimiters and labelled as
  transcript **data**, not instructions.
- The system prompt instructs the model to treat any embedded command as quoted
  speech from a podcast and ignore it.
- Passages are structurally separated from the user's question and the system
  instructions.

**Residual risk — this is the weakest boundary in the system.** An instruction
in a prompt is not a sandbox; a sufficiently crafted passage can influence a
model's output. The mitigation is blast radius, not prevention:

- The agent has **no tools**. It cannot call an API, read a file, or write
  anywhere. A successful injection can distort one answer's text.
- It has **no write access** beyond appending its own turn to the session.
- Citations are validated against retrieved chunks, so injected text cannot
  manufacture a plausible-looking fake source.

An injected passage could produce a misleading answer. It cannot exfiltrate
data or take action.

---

## 4. T4 — Secrets

- Configuration is read from the environment only. No secret is ever a default,
  a literal, or a committed file.
- `.env` is gitignored; only `.env.example` is tracked, with empty values.
- `Settings.describe_safe()` reduces `ANTHROPIC_API_KEY` to a boolean before
  anything is logged.
- `_safe_db_host()` strips credentials from the DSN before it appears in logs.
- Error responses carry `detail` only when `APP_ENV != production`; in
  production the developer context stays in the logs, reachable by request id.
- Driver exceptions are translated at the repository boundary, so a SQLAlchemy
  error string — which can embed the connection URL — never reaches a response.
- Logs record shapes and counts (token estimates, chunk counts, latency), never
  prompt or response bodies.
- Docker images bake no secrets; all config arrives as environment variables.

**Verified by** `test_error_messages_never_leak_the_connection_string`.

**Residual risk:** an operator who sets `LOG_LEVEL=DEBUG` on a third-party
library may get more verbose output than we control. We set known-noisy loggers
(`httpx`, `sqlalchemy.engine`, `uvicorn.access`) to `WARNING` explicitly.

---

## 5. T5 — SQL injection

- All statements are parameterised. No user value is ever string-interpolated
  into SQL.
- `to_tsquery` deserves specific attention: it has its own expression syntax
  (`&`, `|`, `!`, `:`, parentheses) and raises on malformed input.
  `build_or_tsquery()` extracts terms, **strips each to alphanumerics and
  hyphens**, quotes them, and joins with `|`. Raw user text never reaches the
  parser.

**Verified by** `test_escapes_tsquery_operators`.

---

## 6. T7 — Resource limits

| Limit | Value | Why |
|---|---|---|
| Message length | 4,000 chars | Stops a pasted document blowing out context and timeout |
| Artifact size | 200,000 chars | Protects database, payload, and browser |
| Answer tokens | 600 | Bounds latency on a CPU model |
| Essay tokens | 3,000 | Enough for ~1,350 words plus structure |
| Model timeout | 300s (configurable) | Fails cleanly rather than hanging |
| DB pool | 5 + 5 overflow | Bounded connections |
| Retrieval candidates | 40 per arm | Bounded memory and fusion cost |

---

## 7. What we have not done

Honest gaps for a production deployment:

- **No authentication or authorisation.** Every session belongs to
  `local-user`. Anyone who can reach the API can read every conversation. This
  is an explicit v1 assumption for an internal, trusted-network tool (PRD §3,
  A1). The schema is ready; the middleware is not written.
- **No rate limiting.** A local demo does not need it. A shared deployment does
  — put it at the reverse proxy.
- **No audit log** beyond application logs.
- **No encryption at rest** beyond whatever the Postgres deployment provides.
- **No CSRF protection.** Not currently required: the API is stateless, uses no
  cookies, and CORS is origin-restricted with `allow_credentials=False`. Adding
  cookie auth would change that and require CSRF tokens.
- **Dependencies are pinned but not scanned.** `pip-audit` / `npm audit` in CI
  is the obvious next step.

---

## 8. Reporting

This is an assessment submission, not a production service. For a real
deployment: route reports to the owning team, keep the dependency pins current,
and run `pip-audit` and `npm audit` in CI before each release.
