/**
 * Artifact Viewer — renders generated Markdown and HTML/CSS beside the chat.
 *
 * ## Security posture
 *
 * Generated HTML is untrusted. It is rendered in an iframe via `srcDoc` with a
 * deliberately minimal sandbox:
 *
 *     sandbox="allow-popups allow-popups-to-escape-sandbox"
 *
 * What that buys us:
 *
 *  - **No `allow-scripts`** — scripting is disabled inside the frame entirely.
 *    Even if markup survived the backend sanitiser, it cannot execute.
 *  - **No `allow-same-origin`** — the document is assigned a unique opaque
 *    origin. It cannot read this page's DOM, cookies, `localStorage`, or call
 *    the API with the user's context.
 *  - **No `allow-forms`** — a convincing fake login form cannot submit.
 *  - `allow-popups` is granted only so that a legitimate link (for example to a
 *    cited YouTube episode) can open in a new tab.
 *
 * The backend applies the first layer (allow-list sanitisation) and injects a
 * `Content-Security-Policy` meta tag with `default-src 'none'`, so the document
 * cannot reach the network even for images or fonts. See
 * `backend/app/skills/artifact_safety.py` and `docs/security.md` — including
 * the residual risks we do *not* claim to solve.
 *
 * The viewer surfaces the safety report rather than hiding it: if anything was
 * stripped, the user is told what and why.
 */

import { useMemo, useState } from "react";
import type { Artifact } from "../api/types";
import { Markdown } from "./Markdown";

/**
 * Sandbox flags for the artifact frame. Scripts and same-origin are
 * intentionally absent — see the module docstring. Changing this line changes
 * the security posture of the whole feature.
 */
const ARTIFACT_SANDBOX = "allow-popups allow-popups-to-escape-sandbox";

type ViewMode = "preview" | "source";

interface ArtifactViewerProps {
  artifact: Artifact | null;
  artifacts: Artifact[];
  onSelect: (artifact: Artifact) => void;
  onClose: () => void;
  onRegenerate: (artifact: Artifact) => void;
  busy: boolean;
}

export function ArtifactViewer({
  artifact,
  artifacts,
  onSelect,
  onClose,
  onRegenerate,
  busy,
}: ArtifactViewerProps) {
  const [mode, setMode] = useState<ViewMode>("preview");
  const [copied, setCopied] = useState(false);

  const blocked = useMemo(() => {
    const report = artifact?.safety_report;
    if (!report?.blocked) return [];
    return Object.entries(report.blocked).filter(([, count]) => count > 0);
  }, [artifact]);

  if (!artifact) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(artifact.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard can be blocked by permissions policy; failing silently here
      // is better than an error toast for a convenience action.
    }
  };

  const download = () => {
    const extension = artifact.kind === "html" ? "html" : "md";
    const type = artifact.kind === "html" ? "text/html" : "text/markdown";
    const blob = new Blob([artifact.content], { type: `${type};charset=utf-8` });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${slugify(artifact.title)}.${extension}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <aside className="artifact" aria-label="Artifact viewer">
      <header className="artifact__head">
        <div className="artifact__titles">
          <div className="artifact__title" title={artifact.title}>
            {artifact.title}
          </div>
          <div className="artifact__sub">
            {artifact.kind === "html" ? "HTML / CSS" : "Markdown"} · v{artifact.version}
            {artifacts.length > 1 ? ` · ${artifacts.length} artifacts in this chat` : ""}
          </div>
        </div>
        <button className="icon-btn" onClick={onClose} aria-label="Close artifact viewer">
          ✕
        </button>
      </header>

      <div className="artifact__toolbar">
        <div className="segmented" role="group" aria-label="View mode">
          <button
            onClick={() => setMode("preview")}
            aria-pressed={mode === "preview"}
          >
            Preview
          </button>
          <button onClick={() => setMode("source")} aria-pressed={mode === "source"}>
            Source
          </button>
        </div>

        {artifacts.length > 1 && (
          <select
            className="text-btn"
            value={artifact.id}
            onChange={(event) => {
              const next = artifacts.find((a) => a.id === event.target.value);
              if (next) onSelect(next);
            }}
            aria-label="Select artifact"
          >
            {artifacts.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title} (v{item.version})
              </option>
            ))}
          </select>
        )}

        <div className="artifact__spacer" />

        <button className="text-btn" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
        <button className="text-btn" onClick={download}>
          Download
        </button>
        <button
          className="text-btn"
          onClick={() => onRegenerate(artifact)}
          disabled={busy}
          title="Ask the assistant to produce this artifact again"
        >
          Regenerate
        </button>
      </div>

      <div className="artifact__body">
        {blocked.length > 0 && (
          <div className="safety" role="status">
            <div className="safety__title">Sanitised before rendering</div>
            <ul className="safety__list">
              {blocked.map(([kind, count]) => (
                <li key={kind}>
                  {count}× {humanise(kind)}
                </li>
              ))}
            </ul>
            <div style={{ color: "var(--text-tertiary)" }}>
              Rendered without scripts, in a sandboxed frame with network access
              blocked.
            </div>
          </div>
        )}

        {mode === "source" ? (
          <pre className="artifact__code">{artifact.content}</pre>
        ) : artifact.kind === "html" ? (
          <iframe
            className="artifact__frame"
            title={`Artifact preview: ${artifact.title}`}
            sandbox={ARTIFACT_SANDBOX}
            srcDoc={artifact.content}
            referrerPolicy="no-referrer"
          />
        ) : (
          <Markdown content={artifact.content} />
        )}
      </div>
    </aside>
  );
}

function humanise(kind: string): string {
  const labels: Record<string, string> = {
    script_tag: "inline script removed",
    event_handler: "inline event handler removed",
    javascript_url: "javascript: URL removed",
    vbscript_url: "vbscript: URL removed",
    iframe: "nested iframe removed",
    object_embed: "object/embed removed",
    form: "form removed",
    form_control: "form control removed",
    external_link_tag: "external stylesheet link removed",
    meta_tag: "meta tag removed",
    base_tag: "base tag removed",
    css_import: "CSS @import removed",
    css_remote_url: "remote CSS resource removed",
    css_expression: "CSS expression() removed",
    css_behavior: "CSS behavior removed",
    css_javascript: "javascript: in CSS removed",
  };
  return labels[kind] ?? kind.replace(/_/g, " ");
}

function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "artifact"
  );
}
