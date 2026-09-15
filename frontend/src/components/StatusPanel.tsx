/**
 * System status dialog.
 *
 * Exposes the deep health check and knowledge-base state in the UI rather than
 * leaving them to `curl`. During a demo this answers "is it actually running
 * locally?" in one click, and during handoff it is the first thing a client
 * engineer opens when something looks wrong.
 */

import { useEffect } from "react";
import type { HealthResponse, KnowledgeBaseStats, ModelsResponse } from "../api/types";

interface StatusPanelProps {
  health: HealthResponse | null;
  models: ModelsResponse | null;
  knowledgeBase: KnowledgeBaseStats | null;
  onClose: () => void;
  onRefresh: () => void;
}

export function StatusPanel({
  health,
  models,
  knowledgeBase,
  onClose,
  onRefresh,
}: StatusPanelProps) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="System status"
      style={overlayStyle}
      onClick={onClose}
    >
      <div style={panelStyle} onClick={(event) => event.stopPropagation()}>
        <header style={headerStyle}>
          <strong style={{ fontSize: 15 }}>System status</strong>
          <div style={{ display: "flex", gap: 6 }}>
            <button className="text-btn" onClick={onRefresh}>
              Refresh
            </button>
            <button className="icon-btn" onClick={onClose} aria-label="Close">
              ✕
            </button>
          </div>
        </header>

        <div style={bodyStyle}>
          <Section title="Components">
            {health?.components.length ? (
              health.components.map((component) => (
                <div key={component.name} style={rowStyle}>
                  <span className={`dot dot--${component.status}`} aria-hidden="true" />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 600 }}>{component.name}</div>
                    {component.detail && (
                      <div style={{ color: "var(--text-secondary)", fontSize: 12 }}>
                        {component.detail}
                      </div>
                    )}
                  </div>
                  <span style={{ marginLeft: "auto", color: "var(--text-tertiary)", fontSize: 12 }}>
                    {component.latency_ms ? `${Math.round(component.latency_ms)}ms` : ""}
                  </span>
                </div>
              ))
            ) : (
              <p style={mutedStyle}>Checking…</p>
            )}
          </Section>

          <Section title="Model providers">
            {models?.providers.map((provider) => (
              <div key={provider.name} style={rowStyle}>
                <span
                  className={`dot dot--${provider.available ? "ok" : "degraded"}`}
                  aria-hidden="true"
                />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 600 }}>
                    {provider.name}
                    {provider.is_default && (
                      <span className="tag" style={{ marginLeft: 8 }}>
                        active
                      </span>
                    )}
                  </div>
                  <div style={{ color: "var(--text-secondary)", fontSize: 12 }}>
                    {provider.model ?? provider.reason ?? "—"}
                  </div>
                </div>
              </div>
            ))}
            <p style={mutedStyle}>
              Agent runner: <strong>{models?.agent_runner ?? "—"}</strong>. Switch
              providers with <code>MODEL_PROVIDER</code> in <code>.env</code> and
              restart the API — no code changes.
            </p>
          </Section>

          <Section title="Knowledge base">
            {knowledgeBase ? (
              <>
                <dl style={statsGridStyle}>
                  <Stat label="Episodes" value={knowledgeBase.episodes} />
                  <Stat label="Passages" value={knowledgeBase.chunks} />
                  <Stat label="Embedded" value={knowledgeBase.chunks_with_embeddings} />
                  <Stat label="Index size" value={knowledgeBase.dense_index_size} />
                  <Stat
                    label="Sponsor segments removed"
                    value={knowledgeBase.sponsor_segments_removed}
                  />
                  <Stat
                    label="Sponsor words removed"
                    value={knowledgeBase.sponsor_words_removed}
                  />
                </dl>
                <p style={mutedStyle}>
                  Embedding model: <strong>{knowledgeBase.embedding_model ?? "none"}</strong>
                  {knowledgeBase.last_ingestion_status
                    ? ` · last ingestion: ${knowledgeBase.last_ingestion_status}`
                    : ""}
                </p>
              </>
            ) : (
              <p style={mutedStyle}>Loading…</p>
            )}
          </Section>
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section style={{ display: "grid", gap: 8 }}>
      <h2
        style={{
          margin: 0,
          fontSize: 11,
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          color: "var(--text-tertiary)",
        }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt style={{ fontSize: 11, color: "var(--text-tertiary)" }}>{label}</dt>
      <dd style={{ margin: 0, fontSize: 18, fontWeight: 650 }}>
        {value.toLocaleString()}
      </dd>
    </div>
  );
}

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.45)",
  display: "grid",
  placeItems: "center",
  padding: 16,
  zIndex: 50,
};

const panelStyle: React.CSSProperties = {
  width: "min(560px, 100%)",
  maxHeight: "85vh",
  display: "grid",
  gridTemplateRows: "auto minmax(0,1fr)",
  background: "var(--bg-panel)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius-lg)",
  boxShadow: "var(--shadow-pop)",
  overflow: "hidden",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "14px 16px",
  borderBottom: "1px solid var(--border)",
};

const bodyStyle: React.CSSProperties = {
  overflowY: "auto",
  padding: 16,
  display: "grid",
  gap: 20,
};

const rowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 10,
  padding: "8px 10px",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius-sm)",
  fontSize: 13,
};

const statsGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
  gap: 12,
  margin: 0,
};

const mutedStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 12,
  color: "var(--text-secondary)",
};
