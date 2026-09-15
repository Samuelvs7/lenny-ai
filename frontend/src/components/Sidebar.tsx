/**
 * Conversation sidebar: new chat, session history, and system status.
 *
 * Sessions are grouped by recency ("Today", "Yesterday", …) because a flat
 * reverse-chronological list stops being scannable after about a dozen rows,
 * and finding a past conversation is the whole job of this panel.
 */

import type { HealthResponse, ModelsResponse, SessionSummary } from "../api/types";

interface SidebarProps {
  sessions: SessionSummary[];
  activeId: string | null;
  loading: boolean;
  busy: boolean;
  health: HealthResponse | null;
  models: ModelsResponse | null;
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
  onNewChat,
  onSelect,
  onDelete,
  onOpenStatus,
}: SidebarProps) {
  const groups = groupByRecency(sessions);

  return (
    <nav className="sidebar" aria-label="Conversations">
      <div className="sidebar__brand">
        <div className="sidebar__mark" aria-hidden="true">
          L
        </div>
        <div>
          <div className="sidebar__title">Lenny Growth Assistant</div>
          <div className="sidebar__tagline">Grounded in the podcast archive</div>
        </div>
      </div>

      <button className="sidebar__new" onClick={onNewChat} disabled={busy}>
        <span aria-hidden="true">＋</span> New chat
      </button>

      <div className="sidebar__scroll">
        {loading && sessions.length === 0 && (
          <div className="sidebar__group-label">Loading…</div>
        )}

        {!loading && sessions.length === 0 && (
          <p
            style={{
              padding: "8px 10px",
              fontSize: 12.5,
              color: "var(--text-tertiary)",
              margin: 0,
            }}
          >
            No conversations yet. Start one to see it here.
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
                <span className="session__title" title={session.title}>
                  {session.title}
                  <span className="session__meta">
                    {" "}
                    · {session.message_count}
                  </span>
                </span>
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
        <button className="status" onClick={onOpenStatus} aria-label="Open system status">
          <span className={`dot dot--${health?.status ?? "down"}`} aria-hidden="true" />
          <span className="status__text">
            Model:{" "}
            <span className="status__model">
              {models?.active_provider ?? "…"}
              {models?.active_model ? ` · ${models.active_model}` : ""}
            </span>
          </span>
        </button>
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
    "This week": [],
    Earlier: [],
  };

  for (const session of sessions) {
    const age = now - new Date(session.updated_at).getTime();
    if (age < day) buckets.Today.push(session);
    else if (age < 2 * day) buckets.Yesterday.push(session);
    else if (age < 7 * day) buckets["This week"].push(session);
    else buckets.Earlier.push(session);
  }

  return Object.entries(buckets).filter(([, items]) => items.length > 0);
}
