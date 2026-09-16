/**
 * Conversation sidebar: brand, search, session history, and system entry points.
 *
 * Sessions are grouped by recency and stamped with a time, because a flat
 * reverse-chronological list of similar-looking titles stops being scannable
 * after about a dozen rows — and finding a past conversation is this panel's
 * entire job. Search covers the case where grouping is not enough.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  HealthResponse,
  KnowledgeBaseStats,
  ModelsResponse,
  SessionSummary,
} from "../api/types";

interface SidebarProps {
  sessions: SessionSummary[];
  activeId: string | null;
  loading: boolean;
  busy: boolean;
  health: HealthResponse | null;
  models: ModelsResponse | null;
  knowledgeBase: KnowledgeBaseStats | null;
  onNewChat: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onOpenStatus: () => void;
}

export function Sidebar({
  sessions,
  activeId,
  loading,
  busy,
  health,
  models,
  knowledgeBase,
  onNewChat,
  onSelect,
  onDelete,
  onOpenStatus,
}: SidebarProps) {
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  // Ctrl/Cmd+K focuses search — the shortcut users already expect from every
  // other tool with a conversation list.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchRef.current?.focus();
        searchRef.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return sessions;
    return sessions.filter(
      (session) =>
        session.title.toLowerCase().includes(needle) ||
        (session.last_message_preview ?? "").toLowerCase().includes(needle),
    );
  }, [sessions, query]);

  const groups = useMemo(() => groupByRecency(filtered), [filtered]);

  return (
    <nav className="sidebar" aria-label="Conversations">
      <div className="sidebar__brand">
        <BrandMark />
        <div className="sidebar__brandtext">
          <div className="sidebar__title">Lenny Growth Assistant</div>
          <div className="sidebar__tagline">Insights from Lenny's Podcast</div>
        </div>
      </div>

      <div className="sidebar__search">
        <SearchIcon />
        <input
          ref={searchRef}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search conversations..."
          aria-label="Search conversations"
        />
        <kbd className="kbd-hint">Ctrl K</kbd>
      </div>

      <button className="sidebar__new" onClick={onNewChat} disabled={busy}>
        <span aria-hidden="true">+</span> New Chat
      </button>

      <div className="sidebar__scroll">
        {loading && sessions.length === 0 && (
          <div className="sidebar__group-label">Loading…</div>
        )}

        {!loading && sessions.length === 0 && (
          <p className="sidebar__empty">
            No conversations yet. Start one to see it here.
          </p>
        )}

        {!loading && sessions.length > 0 && filtered.length === 0 && (
          <p className="sidebar__empty">
            Nothing matches “{query}”.
          </p>
        )}

        {groups.map(([label, items]) => (
          <div key={label}>
            <div className="sidebar__group-label">{label}</div>
            {items.map((session) => (
              <div
                key={session.id}
                className="session"
                aria-current={session.id === activeId}
                role="button"
                tabIndex={0}
                onClick={() => onSelect(session.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(session.id);
                  }
                }}
              >
                <DocIcon />
                <span className="session__title" title={session.title}>
                  {session.title}
                </span>
                <span className="session__time">{formatStamp(session.updated_at)}</span>
                <button
                  className="session__delete"
                  aria-label={`Delete conversation: ${session.title}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onDelete(session.id);
                  }}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        ))}
      </div>

      <div className="sidebar__footer">
        <button
          className="sidebar__model"
          onClick={onOpenStatus}
          aria-label="Model and system status"
        >
          <span className={`dot dot--${health?.status ?? "down"}`} aria-hidden="true" />
          <span className="sidebar__model-label">Model</span>
          <span className="sidebar__model-name">
            {models?.active_provider ?? "…"}
            {models?.active_model ? ` · ${models.active_model}` : ""}
          </span>
          <ChevronIcon />
        </button>

        <div className="sidebar__nav">
          <button className="navitem" onClick={onOpenStatus}>
            <GearIcon />
            <span>Settings</span>
          </button>
          <button className="navitem" onClick={onOpenStatus}>
            <DatabaseIcon />
            <span>Knowledge Base</span>
            {knowledgeBase && (
              <span className="navitem__badge">{knowledgeBase.episodes} eps</span>
            )}
          </button>
          <a
            className="navitem"
            href="https://github.com/ChatPRD/lennys-podcast-transcripts"
            target="_blank"
            rel="noopener noreferrer"
          >
            <HelpIcon />
            <span>Help &amp; Feedback</span>
          </a>
        </div>
      </div>
    </nav>
  );
}

/** Bucket sessions into human recency labels. */
function groupByRecency(sessions: SessionSummary[]): [string, SessionSummary[]][] {
  const now = Date.now();
  const day = 86_400_000;
  const buckets: Record<string, SessionSummary[]> = {
    Today: [],
    Yesterday: [],
    "This Week": [],
    Older: [],
  };

  for (const session of sessions) {
    const age = now - new Date(session.updated_at).getTime();
    if (age < day) buckets.Today.push(session);
    else if (age < 2 * day) buckets.Yesterday.push(session);
    else if (age < 7 * day) buckets["This Week"].push(session);
    else buckets.Older.push(session);
  }

  return Object.entries(buckets).filter(([, items]) => items.length > 0);
}

/**
 * Time for today's sessions, date for older ones — the detail that is actually
 * useful at each age.
 */
function formatStamp(iso: string): string {
  const date = new Date(iso);
  const age = Date.now() - date.getTime();
  if (age < 86_400_000) {
    return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// --- icons: inline SVG so there is no icon-font or CDN dependency -----------

function BrandMark() {
  return (
    <svg className="brandmark" viewBox="0 0 24 24" aria-hidden="true">
      <rect x="3" y="13" width="4" height="8" rx="1.2" />
      <rect x="10" y="8" width="4" height="13" rx="1.2" />
      <rect x="17" y="3" width="4" height="18" rx="1.2" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="2" />
      <path d="m20 20-3.5-3.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function DocIcon() {
  return (
    <svg className="icon icon--doc" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      <path d="M14 3v5h5" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg className="icon icon--chevron" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="m6 9 6 6 6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function GearIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.8" />
      <path
        d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"
        stroke="currentColor"
        strokeWidth="1.5"
      />
    </svg>
  );
}

function DatabaseIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <ellipse cx="12" cy="5" rx="8" ry="3" stroke="currentColor" strokeWidth="1.8" />
      <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" stroke="currentColor" strokeWidth="1.8" />
      <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  );
}

function HelpIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.8" />
      <path
        d="M9.5 9.5a2.5 2.5 0 1 1 3.6 2.24c-.7.35-1.1.9-1.1 1.76"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <circle cx="12" cy="17" r="1" fill="currentColor" />
    </svg>
  );
}
