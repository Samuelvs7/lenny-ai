/**
 * Types mirroring the backend contracts in `backend/app/api/schemas.py`.
 * Keep them in sync when the API changes.
 */

export type MessageRole = "user" | "assistant" | "system";
export type SkillName = "grounded_qa" | "ship30_essay" | "artifact_gen";
export type ArtifactKind = "markdown" | "html";
export type HealthStatus = "ok" | "degraded" | "down";

export interface Citation {
  marker: string;
  chunk_id: number;
  episode_slug: string;
  guest: string | null;
  title: string;
  youtube_url: string | null;
  /** YouTube URL seeked to the moment the passage is spoken. */
  deep_link: string | null;
  start_seconds: number | null;
  speaker: string | null;
  quote: string;
  score: number;
}

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  skill: SkillName | null;
  router_reason: string | null;
  model_provider: string | null;
  model_name: string | null;
  latency_ms: number | null;
  citations: Citation[];
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface Artifact {
  id: string;
  session_id: string;
  message_id: string | null;
  kind: ArtifactKind;
  title: string;
  content: string;
  version: number;
  safety_report: SafetyReport;
  created_at: string;
}

export interface SafetyReport {
  sanitised?: boolean;
  blocked?: Record<string, number>;
  blocked_total?: number;
  original_length?: number;
  final_length?: number;
  truncated?: boolean;
  policy?: {
    csp?: string;
    iframe_sandbox?: string;
    scripts_enabled?: boolean;
    same_origin?: boolean;
  };
}

export interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_preview: string | null;
}

export interface SessionDetail {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  model_provider: string | null;
  model_name: string | null;
  messages: Message[];
  artifacts: Artifact[];
}

export interface RouteInfo {
  skill: SkillName;
  reason: string;
  method: string;
  artifact_kind: ArtifactKind | null;
}

export interface ChatResponse {
  session_id: string;
  user_message: Message;
  assistant_message: Message;
  artifact: Artifact | null;
  route: RouteInfo;
  citations: Citation[];
  latency_ms: number;
  declined: boolean;
}

export interface ProviderInfo {
  name: string;
  available: boolean;
  model: string | null;
  is_default: boolean;
  reason: string | null;
}

export interface ModelsResponse {
  active_provider: string;
  active_model: string | null;
  agent_runner: string;
  agent_runner_available?: boolean;
  providers: ProviderInfo[];
}

export interface ComponentHealth {
  name: string;
  status: HealthStatus;
  detail: string | null;
  latency_ms: number | null;
}

export interface HealthResponse {
  status: HealthStatus;
  version: string;
  components: ComponentHealth[];
}

export interface KnowledgeBaseStats {
  episodes: number;
  chunks: number;
  chunks_with_embeddings: number;
  sponsor_segments_removed: number;
  sponsor_words_removed: number;
  embedding_model: string | null;
  dense_index_size: number;
  last_ingestion_at: string | null;
  last_ingestion_status: string | null;
}

/** Structured error body returned by the API. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    retryable: boolean;
    request_id: string;
    detail?: string;
  };
}
