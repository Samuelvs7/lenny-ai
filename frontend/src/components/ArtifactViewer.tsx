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
import type { Artifact, Citation } from "../api/types";
import { Markdown } from "./Markdown";

/**
 * Sandbox flags for the artifact frame. Scripts and same-origin are
 * intentionally absent — see the module docstring. Changing this line changes
 * the security posture of the whole feature.
 */
const ARTIFACT_SANDBOX = "allow-popups allow-popups-to-escape-sandbox";

type ViewMode = "preview" | "source";
type Panel = "artifact" | "sources";

interface ArtifactViewerProps {
  artifact: Artifact | null;
  artifacts: Artifact[];
  citations: Citation[];
  panel: Panel;
  onPanelChange: (panel: Panel) => void;
  expanded: boolean;
  onToggleExpand: () => void;
  onSelect: (artifact: Artifact) => void;
  onClose: () => void;
  onRegenerate: (artifact: Artifact) => void;
  busy: boolean;
}

export function ArtifactViewer({
  artifact,
  artifacts,
  citations,
  panel,
  onPanelChange,
  expanded,
  onToggleExpand,
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

  // The panel is also the place sources open into, so it can be shown with
  // citations but no artifact yet.
  if (!artifact && citations.length === 0) return null;

  const copy = async () => {
    if (!artifact) return;
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
    if (!artifact) return;
    const extension = artifact.kind === "html" ? "html" : "md";
    const type = artifact.kind === "html" ? "text/html" : "text/markdown";
    const blob = new Blob([artifact.content], { type: type + ";charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = slugify(artifact.title) + "." + extension;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  const showing: Panel = artifact ? panel : "sources";

  return (
    <aside className="artifact" aria-label="Artifact and sources">
      <header className="artifact__head">
        <div className="tabs" role="tablist" aria-label="Panel">
          <button
            role="tab"
            aria-selected={showing === "artifact"}
            disabled={!artifact}
            onClick={() => onPanelChange("artifact")}
          >
            Artifact
          </button>
          <button
            role="tab"
            aria-selected={showing === "sources"}
            onClick={() => onPanelChange("sources")}
          >
            Sources ({citations.length})
          </button>
        </div>
        <div className="artifact__headactions">
          <button
            className="icon-btn"
            onClick={onToggleExpand}
            aria-pressed={expanded}
            aria-label={expanded ? "Collapse panel" : "Expand panel"}
            title={expanded ? "Collapse panel" : "Expand panel"}
          >
            <ExpandIcon />
          </button>
          <button className="icon-btn" onClick={onClose} aria-label="Close panel">
            <CloseIcon />
          </button>
        </div>
      </header>

      {showing === "sources" ? (
        <SourcesPanel citations={citations} />
      ) : artifact ? (
        <>
          <div className="artifact__toolbar">
            <div className="segmented" role="group" aria-label="View mode">
              <button onClick={() => setMode("preview")} aria-pressed={mode === "preview"}>
                {artifact.kind === "html" ? "HTML" : "Markdown"}
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
              // Framed as a confirmation, not a warning: nothing is broken --
              // the protections did their job. An amber alert here reads as an
              // error and teaches users to distrust a working safety feature.
              <div className="safety" role="status">
                <div className="safety__head">
                  <ShieldIcon />
                  <span className="safety__title">Sanitised before rendering</span>
                </div>
                <ul className="safety__list">
                  {blocked.map(([kind, count]) => (
                    <li key={kind}>
                      <TickIcon />
                      {count > 1 ? count + "\u00d7 " : ""}
                      {humanise(kind)}
                    </li>
                  ))}
                  <li>
                    <TickIcon />
                    Rendered in a sandboxed iframe
                  </li>
                </ul>
              </div>
            )}

            {mode === "source" ? (
              <pre className="artifact__code">{artifact.content}</pre>
            ) : artifact.kind === "html" ? (
              <iframe
                className="artifact__frame"
                title={"Artifact preview: " + artifact.title}
                sandbox={ARTIFACT_SANDBOX}
                srcDoc={artifact.content}
                referrerPolicy="no-referrer"
              />
            ) : (
              <Markdown content={artifact.content} />
            )}
          </div>
        </>
      ) : null}
    </aside>
  );
}

/** Full source list -- the "View all" destination from the chat rail. */
function SourcesPanel({ citations }: { citations: Citation[] }) {
  if (citations.length === 0) {
    return (
      <div className="artifact__body">
        <p className="panel__empty">
          No sources yet. Ask a question and the passages behind the answer appear here.
        </p>
      </div>
    );
  }
  return (
    <div className="artifact__body artifact__body--scroll">
      <ol className="srclist">
        {citations.map((c) => {
          const href = c.deep_link ?? c.youtube_url ?? undefined;
          return (
            <li key={c.marker + "-" + c.chunk_id} className="srclist__item">
              <div className="srclist__top">
                <span className="source__marker">{c.marker}</span>
                <span className="srclist__guest">{c.guest ?? "Unknown guest"}</span>
              </div>
              <div className="srclist__title">{c.title}</div>
              {c.quote && <blockquote className="srclist__quote">{c.quote}</blockquote>}
              <div className="srclist__foot">
                <span>
                  {c.speaker ?? "\u2014"} {"\u00b7"} {formatStamp(c.start_seconds)}
                </span>
                {href && (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    Open at this moment {"\u2192"}
                  </a>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function formatStamp(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "timestamp unavailable";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s2 = seconds % 60;
  const pad = (v: number) => String(v).padStart(2, "0");
  return h > 0 ? h + ":" + pad(m) + ":" + pad(s2) : m + ":" + pad(s2);
}

function ShieldIcon() {
  return (
    <svg className="icon icon--sm safety__icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3l7 3v6c0 4.4-3 7.7-7 9-4-1.3-7-4.6-7-9V6l7-3Z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      <path
        d="m9 12 2 2 4-4"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function TickIcon() {
  return (
    <svg className="icon icon--xs" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="m5 13 4 4L19 7"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ExpandIcon() {
  return (
    <svg className="icon icon--sm" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9 4H4v5M15 20h5v-5M20 9V4h-5M4 15v5h5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg className="icon icon--sm" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="m6 6 12 12M18 6 6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
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
