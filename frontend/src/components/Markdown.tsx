/**
 * Markdown rendering.
 *
 * Two safety measures, because Markdown permits raw HTML and the content here
 * is model-generated:
 *
 *  1. `marked` is configured with raw HTML disabled where possible, and
 *  2. every rendered string passes through DOMPurify before it reaches
 *     `dangerouslySetInnerHTML`.
 *
 * The backend also sanitises before storing (see `artifact_safety.py`). This is
 * the second layer: the renderer must not assume its input was cleaned, because
 * a future caller might pass something that was not.
 */

import { useMemo } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import type { Citation } from "../api/types";

marked.setOptions({ gfm: true, breaks: false });

/** Tags/attributes permitted in rendered Markdown. */
const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    "p", "br", "hr", "strong", "b", "em", "i", "u", "s", "del", "mark", "small",
    "sub", "sup", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "blockquote", "q", "cite", "code", "pre", "kbd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "a", "span", "div",
  ],
  ALLOWED_ATTR: ["href", "title", "target", "rel", "class", "colspan", "rowspan", "align"],
  ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
  FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "input"],
  FORBID_ATTR: ["onerror", "onload", "onclick", "style"],
};

/**
 * Turn `[S1]` markers into links to the cited source.
 *
 * Done on the HTML *after* sanitisation-safe rendering but before purify, using
 * a whitelist of known markers only — a marker that does not resolve to a real
 * citation is left as plain text rather than linked to nothing.
 */
function linkCitations(html: string, citations: Citation[]): string {
  if (citations.length === 0) return html;
  const byMarker = new Map(citations.map((c) => [c.marker.toUpperCase(), c]));

  return html.replace(/\[(S\d+)\]/gi, (match, rawMarker: string) => {
    const citation = byMarker.get(rawMarker.toUpperCase());
    if (!citation) return match;
    const href = citation.deep_link ?? citation.youtube_url;
    const label = `${citation.guest ?? "Source"} — ${citation.title}`;
    if (!href) {
      return `<span class="cite" title="${escapeAttr(label)}">${rawMarker.toUpperCase()}</span>`;
    }
    return `<a class="cite" href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer" title="${escapeAttr(
      label,
    )}">${rawMarker.toUpperCase()}</a>`;
  });
}

function escapeAttr(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

interface MarkdownProps {
  content: string;
  citations?: Citation[];
  className?: string;
}

export function Markdown({ content, citations = [], className = "md" }: MarkdownProps) {
  const html = useMemo(() => {
    const rendered = marked.parse(content, { async: false }) as string;
    const linked = linkCitations(rendered, citations);
    return String(DOMPurify.sanitize(linked, PURIFY_CONFIG));
  }, [content, citations]);

  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />;
}
