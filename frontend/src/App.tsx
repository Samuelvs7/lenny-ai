/**
 * Application shell: owns session state and coordinates the three panes.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "./api/client";
import type {
  Artifact,
  Citation,
  HealthResponse,
  KnowledgeBaseStats,
  Message,
  ModelsResponse,
  SessionSummary,
} from "./api/types";
import { ArtifactViewer } from "./components/ArtifactViewer";
import { Chat } from "./components/Chat";
import { Sidebar } from "./components/Sidebar";
import { StatusPanel } from "./components/StatusPanel";

type Theme = "light" | "dark";
type Panel = "artifact" | "sources";

export function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [activeArtifactId, setActiveArtifactId] = useState<string | null>(null);

  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [models, setModels] = useState<ModelsResponse | null>(null);
  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBaseStats | null>(null);
  const [statusOpen, setStatusOpen] = useState(false);

  const [panelOpen, setPanelOpen] = useState(false);
  const [panel, setPanel] = useState<Panel>("artifact");
  const [expanded, setExpanded] = useState(false);

  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme());
  const [shared, setShared] = useState(false);

  /** The message text of the last send, so "Try again" can replay it. */
  const lastSent = useRef<string | null>(null);

  // --- theme ---------------------------------------------------------------

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("lenny-theme", theme);
    } catch {
      // Private mode / blocked storage: theme simply does not persist.
    }
  }, [theme]);

  // --- bootstrap -----------------------------------------------------------

  const refreshStatus = useCallback(async () => {
    const [healthResult, modelsResult, kbResult] = await Promise.allSettled([
      api.health(),
      api.models(),
      api.knowledgeBase(),
    ]);
    if (healthResult.status === "fulfilled") setHealth(healthResult.value);
    if (modelsResult.status === "fulfilled") setModels(modelsResult.value);
    if (kbResult.status === "fulfilled") setKnowledgeBase(kbResult.value);
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await api.listSessions());
    } catch (err) {
      if (err instanceof ApiError) setError(err);
    } finally {
      setLoadingSessions(false);
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
    void refreshStatus();
  }, [refreshSessions, refreshStatus]);

  // --- session actions -----------------------------------------------------

  const openSession = useCallback(async (id: string) => {
    setError(null);
    setSidebarOpen(false);
    try {
      const detail = await api.getSession(id);
      setActiveId(detail.id);
      setMessages(detail.messages);
      setArtifacts(detail.artifacts);
      setActiveArtifactId(detail.artifacts[0]?.id ?? null);
      setPanelOpen(detail.artifacts.length > 0);
      setPanel(detail.artifacts.length > 0 ? "artifact" : "sources");
    } catch (err) {
      if (err instanceof ApiError) setError(err);
    }
  }, []);

  const newChat = useCallback(async () => {
    setError(null);
    setSidebarOpen(false);
    try {
      const session = await api.createSession();
      setActiveId(session.id);
      setMessages([]);
      setArtifacts([]);
      setActiveArtifactId(null);
      setPanelOpen(false);
      setDraft("");
      await refreshSessions();
      return session.id;
    } catch (err) {
      if (err instanceof ApiError) setError(err);
      return null;
    }
  }, [refreshSessions]);

  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await api.deleteSession(id);
        if (id === activeId) {
          setActiveId(null);
          setMessages([]);
          setArtifacts([]);
          setActiveArtifactId(null);
          setPanelOpen(false);
        }
        await refreshSessions();
      } catch (err) {
        if (err instanceof ApiError) setError(err);
      }
    },
    [activeId, refreshSessions],
  );

  const renameSession = useCallback(async () => {
    if (!activeId) return;
    const current = sessions.find((s) => s.id === activeId)?.title ?? "";
    const next = window.prompt("Rename this conversation", current);
    if (!next || next.trim() === current) return;
    try {
      await api.renameSession(activeId, next.trim());
      await refreshSessions();
    } catch (err) {
      if (err instanceof ApiError) setError(err);
    }
  }, [activeId, sessions, refreshSessions]);

  /**
   * Copy the conversation as Markdown.
   *
   * Deliberately a local export rather than a hosted share link: publishing a
   * conversation would need auth, access control and a public surface, none of
   * which this build has (see PRD non-goals).
   */
  const shareConversation = useCallback(async () => {
    const lines = messages.map((m) => {
      const who = m.role === "user" ? "**You**" : "**Lenny-AI**";
      const cites = (m.citations ?? [])
        .map((c) => `- [${c.marker}] ${c.guest ?? "Unknown"} — ${c.title}${c.deep_link ? ` (${c.deep_link})` : ""}`)
        .join("\n");
      return `${who}\n\n${m.content}${cites ? `\n\nSources:\n${cites}` : ""}`;
    });
    try {
      await navigator.clipboard.writeText(lines.join("\n\n---\n\n"));
      setShared(true);
      setTimeout(() => setShared(false), 1800);
    } catch {
      // Clipboard blocked — nothing useful to show the user for an optional action.
    }
  }, [messages]);

  // --- sending -------------------------------------------------------------

  const send = useCallback(
    async (override?: string) => {
      const text = (override ?? draft).trim();
      if (!text || busy) return;

      // Starting from the empty state creates the session lazily, so a user
      // who never sends a message does not leave an empty conversation behind.
      let sessionId = activeId;
      if (!sessionId) {
        sessionId = await newChat();
        if (!sessionId) return;
      }

      setError(null);
      setBusy(true);
      setDraft("");
      lastSent.current = text;

      // Optimistic echo: the question appears instantly, which matters a lot
      // when the model may take 30+ seconds to answer.
      const optimistic: Message = {
        id: `pending-${Date.now()}`,
        role: "user",
        content: text,
        skill: null,
        router_reason: null,
        model_provider: null,
        model_name: null,
        latency_ms: null,
        citations: [],
        metadata: {},
        created_at: new Date().toISOString(),
      };
      setMessages((current) => [...current, optimistic]);

      try {
        const response = await api.chat(sessionId, text);
        setMessages((current) => [
          ...current.filter((message) => message.id !== optimistic.id),
          response.user_message,
          response.assistant_message,
        ]);

        if (response.artifact) {
          setArtifacts((current) => [response.artifact!, ...current]);
          setActiveArtifactId(response.artifact.id);
          setPanel("artifact");
          setPanelOpen(true);
        }
        void refreshSessions();
      } catch (err) {
        // Roll the optimistic message back so the transcript never shows a
        // question that was never actually recorded.
        setMessages((current) => current.filter((message) => message.id !== optimistic.id));
        setDraft(text);
        if (err instanceof ApiError) setError(err);
        void refreshStatus();
      } finally {
        setBusy(false);
      }
    },
    [activeId, busy, draft, newChat, refreshSessions, refreshStatus],
  );

  const retry = useCallback(() => {
    if (lastSent.current) void send(lastSent.current);
  }, [send]);

  const regenerate = useCallback(
    (artifact: Artifact) => {
      void send(
        `Regenerate the ${artifact.kind === "html" ? "HTML page" : "Markdown document"} "${artifact.title}" with the same grounding, improving clarity and structure.`,
      );
    },
    [send],
  );

  // --- derived -------------------------------------------------------------

  const activeArtifact = useMemo(
    () => artifacts.find((artifact) => artifact.id === activeArtifactId) ?? null,
    [artifacts, activeArtifactId],
  );

  /** Citations from the most recent assistant turn that produced any. */
  const latestCitations: Citation[] = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i];
      if (m.role === "assistant" && (m.citations?.length ?? 0) > 0) return m.citations;
    }
    return [];
  }, [messages]);

  const artifactMessageIds = useMemo(
    () =>
      new Set(
        artifacts
          .map((artifact) => artifact.message_id)
          .filter((id): id is string => Boolean(id)),
      ),
    [artifacts],
  );

  const openArtifactForMessage = useCallback(
    (messageId: string) => {
      const match = artifacts.find((artifact) => artifact.message_id === messageId);
      if (match) {
        setActiveArtifactId(match.id);
        setPanel("artifact");
        setPanelOpen(true);
      }
    },
    [artifacts],
  );

  const viewSources = useCallback(() => {
    setPanel("sources");
    setPanelOpen(true);
  }, []);

  const activeTitle =
    sessions.find((session) => session.id === activeId)?.title ?? "New chat";

  const panelVisible = panelOpen && (activeArtifact !== null || latestCitations.length > 0);

  return (
    <div
      className="app"
      data-artifact-open={panelVisible ? "true" : "false"}
      data-expanded={panelVisible && expanded ? "true" : "false"}
      data-sidebar-open={sidebarOpen ? "true" : "false"}
    >
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        loading={loadingSessions}
        busy={busy}
        health={health}
        models={models}
        knowledgeBase={knowledgeBase}
        onNewChat={() => void newChat()}
        onSelect={(id) => void openSession(id)}
        onDelete={(id) => void deleteSession(id)}
        onOpenStatus={() => {
          void refreshStatus();
          setStatusOpen(true);
        }}
      />

      {sidebarOpen && (
        <button
          className="scrim"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <div className="workspace">
        <header className="topbar">
          <button
            className="icon-btn topbar__menu"
            onClick={() => setSidebarOpen((open) => !open)}
            aria-label="Toggle conversations"
          >
            ☰
          </button>
          <h1 className="topbar__title">{activeTitle}</h1>
          {activeId && (
            <button
              className="icon-btn icon-btn--quiet"
              onClick={() => void renameSession()}
              aria-label="Rename conversation"
              title="Rename conversation"
            >
              <PencilIcon />
            </button>
          )}
          <div className="topbar__spacer" />
          <div className="topbar__actions">
            {messages.length > 0 && (
              <button className="text-btn" onClick={() => void shareConversation()}>
                <ShareIcon />
                {shared ? "Copied" : "Share"}
              </button>
            )}
            <button
              className="icon-btn"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
              title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            >
              {theme === "dark" ? <SunIcon /> : <MoonIcon />}
            </button>
            <span className="topbar__avatar" aria-hidden="true">
              S
            </span>
          </div>
        </header>

        <Chat
          messages={messages}
          busy={busy}
          error={error}
          draft={draft}
          models={models}
          onDraftChange={setDraft}
          onSend={(text) => void send(text)}
          onRetry={retry}
          onOpenArtifact={openArtifactForMessage}
          onViewSources={viewSources}
          artifactMessageIds={artifactMessageIds}
        />
      </div>

      {panelVisible && (
        <ArtifactViewer
          artifact={activeArtifact}
          artifacts={artifacts}
          citations={latestCitations}
          panel={panel}
          onPanelChange={setPanel}
          expanded={expanded}
          onToggleExpand={() => setExpanded((value) => !value)}
          onSelect={(artifact) => {
            setActiveArtifactId(artifact.id);
            setPanel("artifact");
          }}
          onClose={() => setPanelOpen(false)}
          onRegenerate={regenerate}
          busy={busy}
        />
      )}

      {statusOpen && (
        <StatusPanel
          health={health}
          models={models}
          knowledgeBase={knowledgeBase}
          onClose={() => setStatusOpen(false)}
          onRefresh={() => void refreshStatus()}
        />
      )}
    </div>
  );
}

function readStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem("lenny-theme");
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // Storage unavailable — fall through to the system preference.
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// --- icons ------------------------------------------------------------------

function PencilIcon() {
  return (
    <svg className="icon icon--sm" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M4 20h4l10-10a2.8 2.8 0 1 0-4-4L4 16v4Z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ShareIcon() {
  return (
    <svg className="icon icon--sm" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="18" cy="5" r="2.5" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="6" cy="12" r="2.5" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="18" cy="19" r="2.5" stroke="currentColor" strokeWidth="1.8" />
      <path d="m8.4 10.8 7.2-4.1M8.4 13.2l7.2 4.1" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg className="icon icon--sm" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.8" />
      <path
        d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg className="icon icon--sm" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
    </svg>
  );
}
