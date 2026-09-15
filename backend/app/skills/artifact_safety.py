"""Artifact sanitisation.

Generated HTML is **untrusted input**. It is produced by a language model whose
output is shaped by retrieved transcript text, which in turn comes from a
third-party repository. Treating it as trusted would mean any of those layers
could inject script into the application's origin.

The defence is layered, because any single layer can be wrong:

**Layer 1 — server-side sanitisation (this module).**
The document is taken apart, the body is cleaned with ``nh3`` (the Rust
``ammonia`` library) against an explicit allow-list, CSS is filtered, and the
document is rebuilt inside a shell we control. An allow-list is used rather
than a block-list: a block-list has to anticipate every attack, an allow-list
only has to name what is permitted.

**Layer 2 — a Content-Security-Policy meta tag** injected into the rebuilt
document: ``default-src 'none'`` with inline styles and ``data:`` images only.
Even if markup slipped through, it cannot reach the network.

**Layer 3 — a sandboxed iframe** in the frontend, *without* ``allow-scripts``
and *without* ``allow-same-origin``. The document lands in an opaque origin
with scripting disabled, so it cannot touch the parent DOM, read cookies or
``localStorage``, or call the API.

What is allowed: structural HTML, text, tables, lists, links (http/https/
mailto, forced to ``target="_blank" rel="noopener noreferrer"``), inline and
block CSS, and ``data:`` images.

What is blocked, and why:

===========================  ==================================================
Blocked                      Reason
===========================  ==================================================
``<script>``, ``on*``        Script execution is the primary XSS vector.
``javascript:`` URLs         Script execution via navigation.
``<iframe>``/``<object>``/   Nested browsing contexts can escape the intended
``<embed>``                  rendering surface and load remote content.
``<form>``, ``<input>``      A form that looks like the real UI is a credential
                             phishing surface.
``<link>``, ``<meta>``,      Remote stylesheets, redirects, and base-URL
``<base>``                   hijacking.
External ``src``/``href``    Any network fetch is a data-exfiltration channel
in resources                 (blocked by CSP as well).
CSS ``@import``, ``url()``   Same: CSS can fetch remote resources.
with remote schemes
===========================  ==================================================

**Residual risks — stated honestly, not hidden.** This is a reasonable posture,
not a guarantee:

* A defect in ``nh3``/``ammonia`` upstream would weaken layer 1. Layers 2 and 3
  are what make that survivable.
* Sandboxed static HTML can still render *misleading* content — a convincing
  fake login form built purely from CSS, for example. It cannot submit or
  execute anything, but a user could still be deceived visually. The viewer
  labels artifacts as generated content to mitigate this socially.
* CSS can consume significant CPU (huge animations, pathological selectors).
  The iframe confines the damage to itself and the user can close the panel.
* ``data:`` images are permitted for usability; a large one could bloat the
  page. Size is bounded by :data:`MAX_ARTIFACT_CHARS`.

We do not claim this is "secure". We claim it is layered, explicit, and that
the trade-offs above were chosen deliberately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import nh3

from app.observability import get_logger

log = get_logger(__name__)

#: Upper bound on a stored artifact. Protects the database, the API payload,
#: and the browser from a runaway generation.
MAX_ARTIFACT_CHARS = 200_000

#: Tags permitted in artifact bodies.
ALLOWED_TAGS: set[str] = {
    "div", "span", "p", "br", "hr", "section", "article", "header", "footer",
    "main", "aside", "nav", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "a", "strong", "b", "em", "i", "u", "s", "small", "mark", "sub", "sup",
    "blockquote", "q", "cite", "code", "pre", "kbd", "samp", "var", "abbr", "time",
    "img", "picture", "source",
    "details", "summary", "progress", "meter",
    "svg", "path", "circle", "rect", "line", "polyline", "polygon", "g", "text",
}

#: Attributes permitted, per tag. ``*`` applies to every allowed tag.
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "*": {"class", "id", "style", "title", "role", "aria-label", "aria-hidden", "lang"},
    # No "rel" here on purpose: nh3 sets it from ``link_rel`` below and panics
    # if the attribute is also allow-listed. Every link therefore gets
    # rel="noopener noreferrer" applied by the sanitiser, not by the model.
    "a": {"href", "target"},
    "img": {"src", "alt", "width", "height", "loading"},
    "source": {"srcset", "type", "media"},
    "td": {"colspan", "rowspan", "headers"},
    "th": {"colspan", "rowspan", "scope", "headers"},
    "col": {"span"},
    "colgroup": {"span"},
    "time": {"datetime"},
    "progress": {"value", "max"},
    "meter": {"value", "min", "max", "low", "high", "optimum"},
    "details": {"open"},
    "svg": {"viewBox", "width", "height", "xmlns", "fill", "stroke", "preserveAspectRatio"},
    "path": {"d", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin"},
    "circle": {"cx", "cy", "r", "fill", "stroke", "stroke-width"},
    "rect": {"x", "y", "width", "height", "rx", "ry", "fill", "stroke", "stroke-width"},
    "line": {"x1", "y1", "x2", "y2", "stroke", "stroke-width"},
    "polyline": {"points", "fill", "stroke", "stroke-width"},
    "polygon": {"points", "fill", "stroke", "stroke-width"},
    "g": {"fill", "stroke", "transform"},
    "text": {"x", "y", "fill", "font-size", "text-anchor"},
}

#: URL schemes allowed in ``href``. Note the absence of ``javascript``.
ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto", "data"}

#: Content-Security-Policy applied inside the artifact document.
ARTIFACT_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "font-src 'none'; "
    "script-src 'none'; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-src 'none'"
)

#: Patterns removed from the whole document before parsing. Belt to nh3's
#: braces: these are the constructs whose presence we also want to *count*
#: and report, so the UI can show that something was blocked.
_DANGEROUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("script_tag", re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)),
    ("script_tag", re.compile(r"<script\b[^>]*/?>", re.I)),
    ("iframe", re.compile(r"<iframe\b[^>]*>.*?</iframe\s*>", re.I | re.S)),
    ("iframe", re.compile(r"<iframe\b[^>]*/?>", re.I)),
    ("object_embed", re.compile(r"<(object|embed|applet)\b[^>]*>.*?</\1\s*>", re.I | re.S)),
    ("object_embed", re.compile(r"<(object|embed|applet)\b[^>]*/?>", re.I)),
    ("form", re.compile(r"<form\b[^>]*>.*?</form\s*>", re.I | re.S)),
    ("form_control", re.compile(r"<(input|button|select|textarea|option)\b[^>]*/?>", re.I)),
    ("external_link_tag", re.compile(r"<link\b[^>]*>", re.I)),
    ("meta_tag", re.compile(r"<meta\b[^>]*>", re.I)),
    ("base_tag", re.compile(r"<base\b[^>]*>", re.I)),
    ("event_handler", re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)),
    ("javascript_url", re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I)),
    ("vbscript_url", re.compile(r"(href|src)\s*=\s*([\"'])\s*vbscript:[^\"']*\2", re.I)),
]

#: CSS constructs that can reach the network or execute.
_DANGEROUS_CSS: list[tuple[str, re.Pattern[str]]] = [
    ("css_import", re.compile(r"@import\b[^;]*;?", re.I)),
    ("css_remote_url", re.compile(r"url\(\s*[\"']?\s*(?!data:)[a-z]+:[^)]*\)", re.I)),
    ("css_remote_url", re.compile(r"url\(\s*[\"']?\s*//[^)]*\)", re.I)),
    ("css_expression", re.compile(r"expression\s*\([^)]*\)", re.I)),
    ("css_behavior", re.compile(r"behavior\s*:[^;]+;?", re.I)),
    ("css_javascript", re.compile(r"javascript\s*:", re.I)),
]


@dataclass(slots=True)
class SafetyReport:
    """What sanitisation changed, surfaced in the UI and stored on the artifact."""

    sanitised: bool = False
    blocked: dict[str, int] = field(default_factory=dict)
    original_length: int = 0
    final_length: int = 0
    truncated: bool = False

    def record(self, kind: str, count: int = 1) -> None:
        if count:
            self.blocked[kind] = self.blocked.get(kind, 0) + count
            self.sanitised = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "sanitised": self.sanitised,
            "blocked": self.blocked,
            "blocked_total": sum(self.blocked.values()),
            "original_length": self.original_length,
            "final_length": self.final_length,
            "truncated": self.truncated,
            "policy": {
                "csp": ARTIFACT_CSP,
                "iframe_sandbox": "allow-popups allow-popups-to-escape-sandbox",
                "scripts_enabled": False,
                "same_origin": False,
            },
        }


def _strip_patterns(
    html: str, patterns: list[tuple[str, re.Pattern[str]]], report: SafetyReport
) -> str:
    for kind, pattern in patterns:
        matches = pattern.findall(html)
        if matches:
            report.record(kind, len(matches))
            html = pattern.sub(" ", html)
    return html


def sanitise_css(css: str, report: SafetyReport) -> str:
    """Remove CSS that can fetch remote resources or execute."""
    return _strip_patterns(css, _DANGEROUS_CSS, report)


def _extract_styles(html: str, report: SafetyReport) -> tuple[str, str]:
    """Pull ``<style>`` blocks out of the document.

    They are extracted rather than passed to nh3, which strips ``<style>``
    entirely — and a stripped stylesheet would leave every generated artifact
    unstyled, defeating the point of asking for HTML *and CSS*.
    """
    collected: list[str] = []

    def take(match: re.Match[str]) -> str:
        collected.append(match.group(1))
        return " "

    remaining = re.sub(r"<style\b[^>]*>(.*?)</style\s*>", take, html, flags=re.I | re.S)
    css = sanitise_css("\n".join(collected), report)
    return remaining, css


def _extract_body(html: str) -> str:
    match = re.search(r"<body\b[^>]*>(.*?)</body\s*>", html, re.I | re.S)
    if match:
        return match.group(1)
    # Not a full document: drop any head/html wrappers and use what is left.
    stripped = re.sub(r"</?(?:html|head|body)\b[^>]*>", " ", html, flags=re.I)
    stripped = re.sub(r"<title\b[^>]*>.*?</title\s*>", " ", stripped, flags=re.I | re.S)
    return stripped


def extract_title(html: str, fallback: str = "Generated artifact") -> str:
    """Prefer the document ``<title>``, else the first heading."""
    match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", html, re.I | re.S)
    if match and match.group(1).strip():
        return " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split())[:120]
    heading = re.search(r"<h1\b[^>]*>(.*?)</h1\s*>", html, re.I | re.S)
    if heading:
        text = " ".join(re.sub(r"<[^>]+>", "", heading.group(1)).split())
        if text:
            return text[:120]
    return fallback


def sanitise_html_artifact(raw_html: str) -> tuple[str, SafetyReport]:
    """Clean generated HTML and rebuild it inside a controlled shell.

    Returns the safe document and a report of what was removed.
    """
    report = SafetyReport(original_length=len(raw_html))

    html = raw_html.strip()
    # Models often wrap output in a Markdown fence despite instructions.
    html = re.sub(r"^```(?:html)?\s*", "", html, flags=re.I)
    html = re.sub(r"\s*```$", "", html)

    if len(html) > MAX_ARTIFACT_CHARS:
        html = html[:MAX_ARTIFACT_CHARS]
        report.truncated = True

    title = extract_title(html)
    html, css = _extract_styles(html, report)
    html = _strip_patterns(html, _DANGEROUS_PATTERNS, report)
    body = _extract_body(html)

    cleaned_body = nh3.clean(
        body,
        tags=ALLOWED_TAGS,
        attributes={tag: set(attrs) for tag, attrs in ALLOWED_ATTRIBUTES.items()},
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )

    document = _build_document(title=title, css=css, body=cleaned_body)
    report.final_length = len(document)

    if report.sanitised:
        log.warning(
            "artifact.sanitised",
            blocked=report.blocked,
            total=sum(report.blocked.values()),
            note="unsafe constructs removed from generated HTML",
        )
    return document, report


def _build_document(*, title: str, css: str, body: str) -> str:
    """Assemble the final artifact document with our own head.

    The head is ours, never the model's: that is what guarantees the CSP is
    present and that no ``<meta>``/``<base>``/``<link>`` the model emitted
    survives.
    """
    safe_title = re.sub(r"[<>&\"']", "", title)[:120] or "Generated artifact"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{ARTIFACT_CSP}">
<title>{safe_title}</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; }}
html {{ color-scheme: light; }}
body {{
  margin: 0;
  padding: 24px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6;
  color: #1a1a1a;
  background: #ffffff;
  overflow-wrap: break-word;
}}
img {{ max-width: 100%; height: auto; }}
table {{ border-collapse: collapse; max-width: 100%; }}
pre {{ overflow-x: auto; }}
{css}
</style>
</head>
<body>
{body}
</body>
</html>"""


def sanitise_markdown_artifact(raw_markdown: str) -> tuple[str, SafetyReport]:
    """Clean generated Markdown.

    Markdown permits raw HTML, so the same script/handler vectors apply. The
    frontend additionally renders Markdown without raw-HTML support, but we
    strip here too so that what is *stored* is already safe — anything else
    would make the database the weak link for a future consumer.
    """
    report = SafetyReport(original_length=len(raw_markdown))

    markdown = raw_markdown.strip()
    markdown = re.sub(r"^```(?:markdown|md)?\s*\n", "", markdown, flags=re.I)
    markdown = re.sub(r"\n```$", "", markdown)

    if len(markdown) > MAX_ARTIFACT_CHARS:
        markdown = markdown[:MAX_ARTIFACT_CHARS]
        report.truncated = True

    markdown = _strip_patterns(markdown, _DANGEROUS_PATTERNS, report)
    report.final_length = len(markdown)

    if report.sanitised:
        log.warning("artifact.markdown_sanitised", blocked=report.blocked)
    return markdown, report
