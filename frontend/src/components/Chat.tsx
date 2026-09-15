/**
 * Chat transcript and composer.
 *
 * Interaction states covered here, because a demo that only shows the happy
 * path is not a product: empty, thinking, answered, declined ("I don't have
 * enough…"), and error-with-retry.
 */

import { useEffect, useRef, useState } from "react";
import type { Citation, Message, ModelsResponse } from "../api/types";
import { ApiError } from "../api/client";
import { Markdown } from "./Markdown";

const SUGGESTIONS: { kind: string; text: string }[] = [
  {
    kind: "Grounded answer",
    text: "What did guests say about finding product-market fit?",
  },
  {
    kind: "Grounded answer",
    text: "How do the best product managers make decisions?",
  },
  {
    kind: "Ship 30 essay",
    text: "Write a Ship 30 for 30 essay about early-stage growth tactics",
  },
  {
    kind: "HTML artifact",
    text: "Create an HTML one-pager summarising the growth advice in this chat",
  },
];

interface ChatProps {
  messages: Message[];
  busy: boolean;
  error: ApiError | null;
  draft: string;
  models: ModelsResponse | null;
  onDraftChange: (value: string) => void;
  onSend: (message?: string) => void;
  onRetry: () => void;
  onOpenArtifact: (messageId: string) => void;
  onViewSources: () => void;
  artifactMessageIds: Set<string>;
}

export function Chat({
  messages,
  busy,
  error,
  draft,
  models,
  onDraftChange,
  onSend,
  onRetry,
  onOpenArtifact,
  onViewSources,
  artifactMessageIds,
}: ChatProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, busy]);

  // Grow the composer with its content, up to the CSS max-height.
  //
  // When the field is empty we clear the inline height entirely and let CSS
  // govern, rather than measuring. Measuring an empty textarea proved
  // unreliable: `scrollHeight` is only trustworthy once the grid has resolved
  // its column widths, and on mount it reported 402px for an empty field —
  // pinning the composer permanently open at its 180px maximum. With content
  // present the element is laid out and measurement is sound.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    if (!draft) {
      el.style.height = "";
      return;
    }
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [draft]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter inserts a newline — the convention users expect
    // from every other chat product.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (draft.trim() && !busy) onSend();
    }
  };

  const isEmpty = messages.length === 0;

  return (
    <div className="chat">
      <div className="chat__scroll">
        {isEmpty && !busy ? (
          <EmptyState onPick={(text) => onSend(text)} disabled={busy} />
        ) : (
          <div className="chat__inner">
            {messages.map((message) =>
              message.role === "user" ? (
                <div className="turn turn--user" key={message.id}>
                  <div className="bubble">
                    {message.content}
                    <time className="bubble__time">{formatClock(message.created_at)}</time>
                  </div>
                  <Avatar />
                </div>
              ) : (
                <AssistantTurn
                  key={message.id}
                  message={message}
                  hasArtifact={artifactMessageIds.has(message.id)}
                  onOpenArtifact={() => onOpenArtifact(message.id)}
                  onViewSources={onViewSources}
                />
              ),
            )}

            {busy && (
              <div className="turn">
                <div className="thinking" role="status" aria-live="polite">
                  <span className="thinking__dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  Searching transcripts and composing an answer…
                </div>
              </div>
            )}

            {error && (
              <div className="alert" role="alert">
                <div className="alert__title">{errorTitle(error)}</div>
                <div>{error.message}</div>
                <div className="alert__meta">
                  {error.code} · request {error.requestId}
                </div>
                {error.retryable && (
                  <button className="alert__retry" onClick={onRetry}>
                    Try again
                  </button>
                )}
              </div>
            )}

            <div ref={endRef} />
          </div>
        )}
      </div>

      <div className="composer">
        <div className="composer__inner">
          <div className="composer__box">
            <textarea
              ref={textareaRef}
              value={draft}
              rows={1}
              placeholder={
                isEmpty
                  ? "Ask a question about product, growth or hiring…"
                  : "Ask a follow-up question..."
              }
              aria-label="Message"
              onChange={(event) => onDraftChange(event.target.value)}
              onKeyDown={handleKeyDown}
              disabled={busy}
            />
            <div className="composer__tools">
              <span
                className="composer__model"
                title="Answers are generated by this model. Change it with MODEL_PROVIDER in .env and restart."
              >
                <span className="dot dot--ok" aria-hidden="true" />
                {models?.active_provider ?? "model"}
                {models?.active_model ? ` · ${models.active_model}` : ""}
              </span>
              <button
                className="send"
                onClick={() => onSend()}
                disabled={busy || !draft.trim()}
                aria-label="Send message"
              >
                <ArrowUpIcon />
              </button>
            </div>
          </div>
          <div className="composer__hint">
            <span>Tip: ask for essays, artifacts, or specific examples from episodes.</span>
            <span>
              <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function AssistantTurn({
  message,
  hasArtifact,
  onOpenArtifact,
  onViewSources,
}: {
  message: Message;
  hasArtifact: boolean;
  onOpenArtifact: () => void;
  onViewSources: () => void;
}) {
  const declined = Boolean(message.metadata?.declined);
  const citations = message.citations ?? [];
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard can be blocked by permissions policy; a silent no-op beats
      // an error toast for a convenience action.
    }
  };

  return (
    <div className="turn turn--assistant">
      <BrandAvatar />
      <div className="answer">
        <Markdown content={message.content} citations={citations} />

        {citations.length > 0 && (
          <Sources citations={citations} onViewAll={onViewSources} />
        )}

        <div className="answer__actions">
          <button className="iconaction" onClick={copy} aria-label="Copy answer">
            {copied ? <CheckIcon /> : <CopyIcon />}
          </button>
          {hasArtifact && (
            <button className="textaction" onClick={onOpenArtifact}>
              Open artifact
            </button>
          )}
          <span className="answer__meta">
            {message.skill && (
              <span className="tag tag--skill" title={message.router_reason ?? undefined}>
                {skillLabel(message.skill)}
              </span>
            )}
            {declined && <span className="tag tag--declined">Not enough evidence</span>}
            {message.model_provider && (
              <span className="tag">
                {message.model_provider}
                {message.model_name ? ` · ${message.model_name}` : ""}
              </span>
            )}
            {typeof message.latency_ms === "number" && (
              <span className="answer__latency">
                {(message.latency_ms / 1000).toFixed(1)}s
              </span>
            )}
          </span>
        </div>
      </div>
    </div>
  );
}

function Sources({
  citations,
  onViewAll,
}: {
  citations: Citation[];
  onViewAll: () => void;
}) {
  return (
    <div className="sources">
      <div className="sources__head">
        <LinkIcon />
        <span>Sources ({citations.length})</span>
        <button className="sources__viewall" onClick={onViewAll}>
          View all <span aria-hidden="true">→</span>
        </button>
      </div>
      <div className="sources__list">
        {citations.map((citation) => {
          const href = citation.deep_link ?? citation.youtube_url ?? undefined;
          const Tag = href ? "a" : "div";
          return (
            <Tag
              key={`${citation.marker}-${citation.chunk_id}`}
              className="source"
              {...(href ? { href, target: "_blank", rel: "noopener noreferrer" } : {})}
              title={citation.quote}
            >
              <div className="source__top">
                <span className="source__marker">{citation.marker}</span>
                <span className="source__guest">{citation.guest ?? "Unknown guest"}</span>
              </div>
              <div className="source__title">{citation.title}</div>
              <div className="source__foot">
                <span className="source__stamp">
                  {citation.speaker ?? "—"} · {formatTimestamp(citation.start_seconds)}
                </span>
                {href && <ExternalIcon />}
              </div>
            </Tag>
          );
        })}
      </div>
    </div>
  );
}

function EmptyState({
  onPick,
  disabled,
}: {
  onPick: (text: string) => void;
  disabled: boolean;
}) {
  return (
    <div className="empty">
      <div>
        <h1 className="empty__title">What would you like to know?</h1>
        <p className="empty__lede">
          Ask a product or growth question and get an answer grounded in Lenny's
          Podcast transcripts — every claim cites the episode and the moment it
          was said.
        </p>
      </div>
      <div className="empty__grid">
        {SUGGESTIONS.map((suggestion) => (
          <button
            key={suggestion.text}
            className="suggestion"
            onClick={() => onPick(suggestion.text)}
            disabled={disabled}
          >
            <span className="suggestion__kind">{suggestion.kind}</span>
            <span className="suggestion__text">{suggestion.text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function skillLabel(skill: string): string {
  switch (skill) {
    case "grounded_qa":
      return "Grounded answer";
    case "ship30_essay":
      return "Ship 30 essay";
    case "artifact_gen":
      return "Artifact";
    default:
      return skill;
  }
}

function errorTitle(error: ApiError): string {
  switch (error.code) {
    case "provider_timeout":
    case "client_timeout":
      return "The model took too long";
    case "provider_unavailable":
      return "Model unavailable";
    case "database_unavailable":
      return "Conversation store unavailable";
    case "retrieval_unavailable":
      return "Knowledge base not loaded";
    case "network_error":
      return "Cannot reach the API";
    default:
      return "Something went wrong";
  }
}

function formatTimestamp(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "timestamp unavailable";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

// --- icons: inline SVG, no icon font or CDN dependency ----------------------

function Avatar() {
  return (
    <span className="avatar avatar--user" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="8" r="3.5" stroke="currentColor" strokeWidth="1.8" />
        <path
          d="M5 20a7 7 0 0 1 14 0"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    </span>
  );
}

function BrandAvatar() {
  return (
    <span className="avatar avatar--brand" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <rect x="3" y="13" width="4" height="8" rx="1.2" />
        <rect x="10" y="8" width="4" height="13" rx="1.2" />
        <rect x="17" y="3" width="4" height="18" rx="1.2" />
      </svg>
    </span>
  );
}

function ArrowUpIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 19V5" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
      <path
        d="m6 11 6-6 6 6"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="1.8" />
      <path
        d="M5 15V5a2 2 0 0 1 2-2h10"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="m5 13 4 4L19 7"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function LinkIcon() {
  return (
    <svg className="icon icon--sm" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <path
        d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function ExternalIcon() {
  return (
    <svg
      className="icon icon--sm source__ext"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M14 4h6v6M20 4l-8 8"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}
