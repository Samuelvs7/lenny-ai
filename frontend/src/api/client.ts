/**
 * API client.
 *
 * Every call funnels through `request()` so error handling is uniform: the
 * backend's structured error body is turned into a typed `ApiError` carrying
 * the code, the user-facing message, whether retrying could help, and the
 * request id for correlating with server logs.
 */

import type {
  ApiErrorBody,
  Artifact,
  ChatResponse,
  HealthResponse,
  KnowledgeBaseStats,
  ModelsResponse,
  SessionDetail,
  SessionSummary,
} from "./types";

const BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId: string;
  readonly status: number;
  readonly detail?: string;

  constructor(
    message: string,
    options: {
      code: string;
      retryable: boolean;
      requestId: string;
      status: number;
      detail?: string;
    },
  ) {
    super(message);
    this.name = "ApiError";
    this.code = options.code;
    this.retryable = options.retryable;
    this.requestId = options.requestId;
    this.status = options.status;
    this.detail = options.detail;
  }
}

/** Long enough for a local CPU model to finish an essay. */
const DEFAULT_TIMEOUT_MS = 300_000;

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    });
  } catch (error) {
    clearTimeout(timer);
    // A network failure and an abort are indistinguishable to the user unless
    // we say which happened, and the advice differs for each.
    const aborted = error instanceof DOMException && error.name === "AbortError";
    throw new ApiError(
      aborted
        ? "The request timed out. Local models can be slow — try a shorter question."
        : "Cannot reach the API. Is the backend running on port 8000?",
      {
        code: aborted ? "client_timeout" : "network_error",
        retryable: true,
        requestId: "-",
        status: 0,
      },
    );
  }
  clearTimeout(timer);

  if (response.status === 204) {
    return undefined as T;
  }

  if (!response.ok) {
    const requestId = response.headers.get("X-Request-ID") ?? "-";
    let body: ApiErrorBody | null = null;
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // Non-JSON error page (a proxy, say) — fall through to a generic message.
    }
    throw new ApiError(body?.error?.message ?? `Request failed (${response.status})`, {
      code: body?.error?.code ?? "http_error",
      retryable: body?.error?.retryable ?? response.status >= 500,
      requestId: body?.error?.request_id ?? requestId,
      status: response.status,
      detail: body?.error?.detail,
    });
  }

  return (await response.json()) as T;
}

export const api = {
  health: () => request<HealthResponse>("/health/deep", {}, 60_000),

  models: () => request<ModelsResponse>("/api/models", {}, 15_000),

  knowledgeBase: () => request<KnowledgeBaseStats>("/api/knowledge-base", {}, 15_000),

  listSessions: () => request<SessionSummary[]>("/api/sessions", {}, 15_000),

  createSession: () =>
    request<SessionDetail>("/api/sessions", { method: "POST", body: "{}" }, 15_000),

  getSession: (id: string) => request<SessionDetail>(`/api/sessions/${id}`, {}, 20_000),

  renameSession: (id: string, title: string) =>
    request<void>(
      `/api/sessions/${id}`,
      { method: "PATCH", body: JSON.stringify({ title }) },
      15_000,
    ),

  deleteSession: (id: string) =>
    request<void>(`/api/sessions/${id}`, { method: "DELETE" }, 15_000),

  chat: (sessionId: string, message: string, provider?: string) =>
    request<ChatResponse>(`/api/sessions/${sessionId}/chat`, {
      method: "POST",
      body: JSON.stringify({ message, provider }),
    }),

  listArtifacts: (sessionId: string) =>
    request<Artifact[]>(`/api/sessions/${sessionId}/artifacts`, {}, 20_000),
};
