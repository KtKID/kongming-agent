import type {
  ClaudeProjectSummaryDTO,
  ClaudeProjectsRefreshProgressDTO,
  ErrorCode,
  EvolutionDecisionItemDTO,
  ErrorResponseDTO,
  EvolutionDecisionRequest,
  EvolutionDecisionResponse,
  EvolutionReviewDTO,
  ThreadTaskProgressSnapshot,
} from "@/protocol";

/**
 * kongming-agent v0.1.5 REST 封装
 *
 * ## 安全约束
 *
 * - 所有 mutating 请求自动加 `X-Requested-With: XMLHttpRequest` 头（CSRF 防御 +
 *   防 simple-request 的 form CSRF）
 * - `credentials: "include"`：带上 cookie（dev 走 vite proxy，prod 同源也照样带）
 * - 401 → 调用 `useAuthStore.getState().logoutLocal()` 清状态 + redirect /login
 * - 429 → 抛 `RateLimitedError`，调用方自行 toast 倒计时
 * - 其它非 2xx → 解析 `ErrorResponseDTO` 抛 `ApiError`
 *
 * ## 不允许在前端代码中绕过本封装直接 `fetch()`：
 * - 缺 X-Requested-With → CSRF 暴露
 * - 不复用错误解析逻辑 → UI 错误体验不一致
 */

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export class ApiError extends Error {
  constructor(
    public status: number,
    public errorCode: ErrorCode | string,
    public detail: string,
    public details?: Record<string, unknown>,
  ) {
    super(`[${errorCode}] ${detail}`);
    this.name = "ApiError";
  }
}

export class RateLimitedError extends Error {
  constructor(
    public retryAfterSeconds: number,
    msg: string,
  ) {
    super(msg);
    this.name = "RateLimitedError";
  }
}

/**
 * 401 拦截器。延迟 import auth store 避免循环依赖（auth store 反过来用本模块）。
 */
async function handle401(): Promise<void> {
  // 动态 import 避免与 auth store 循环依赖
  const { useAuthStore } = await import("@/stores/auth");
  useAuthStore.getState().logoutLocal();
  if (
    typeof window !== "undefined" &&
    window.location.pathname !== "/login"
  ) {
    // 用 replace 避免 back stack 累积
    window.location.replace("/login");
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {
    "X-Requested-With": "XMLHttpRequest",
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      credentials: "include",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    // TypeError 表示 DNS / TCP 失败、CORS 拒绝等
    const msg = err instanceof Error ? err.message : String(err);
    throw new ApiError(0, "network", `网络错误：${msg}`);
  }

  if (response.status === 401) {
    await handle401();
    throw new ApiError(401, "unauthenticated", "session expired");
  }

  if (response.status === 429) {
    const retryHeader = response.headers.get("Retry-After");
    const retry = retryHeader ? Number(retryHeader) : 300;
    throw new RateLimitedError(
      Number.isFinite(retry) ? retry : 300,
      "请求过于频繁",
    );
  }

  if (!response.ok) {
    let data: ErrorResponseDTO | null = null;
    try {
      data = (await response.json()) as ErrorResponseDTO;
    } catch {
      // 非 json 错误体；按 statusText 兜底
    }
    throw new ApiError(
      response.status,
      data?.error_code ?? "internal",
      data?.message ?? response.statusText ?? "request failed",
      data?.details,
    );
  }

  if (response.status === 204) return undefined as T;
  // 空 body 也接受为 undefined
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

export const apiGet = <T>(path: string) => request<T>("GET", path);
export const apiPost = <T>(path: string, body?: unknown) =>
  request<T>("POST", path, body);
export const apiPut = <T>(path: string, body?: unknown) =>
  request<T>("PUT", path, body);
export const apiPatch = <T>(path: string, body?: unknown) =>
  request<T>("PATCH", path, body);
export const apiDelete = (path: string) => request<void>("DELETE", path);

export const apiGetThreadTaskProgress = (threadId: string) =>
  apiGet<ThreadTaskProgressSnapshot>(
    `/api/threads/${encodeURIComponent(threadId)}/task-progress`,
  );

function normalizeEvolutionDecision(
  decision: EvolutionDecisionItemDTO,
): EvolutionDecisionItemDTO {
  return {
    ...decision,
    applied_status: decision.applied_status ?? null,
    applied_path: decision.applied_path ?? null,
    applied_mode: decision.applied_mode ?? null,
    applied_at_ms: decision.applied_at_ms ?? null,
    applied_error: decision.applied_error ?? null,
  };
}

function normalizeEvolutionReview(review: EvolutionReviewDTO): EvolutionReviewDTO {
  return {
    ...review,
    decisions: review.decisions.map(normalizeEvolutionDecision),
  };
}

export const apiGetEvolutionReviews = async (threadId: string) => {
  const reviews = await apiGet<EvolutionReviewDTO[]>(
    `/api/threads/${threadId}/evolution/reviews`,
  );
  return reviews.map(normalizeEvolutionReview);
};

export const apiPostEvolutionDecision = (
  threadId: string,
  reviewId: string,
  body: EvolutionDecisionRequest,
) =>
  apiPost<EvolutionDecisionResponse>(
    `/api/threads/${threadId}/evolution/reviews/${encodeURIComponent(reviewId)}/decisions`,
    body,
  ).then((response) => ({
    ...response,
    review: normalizeEvolutionReview(response.review),
  }));

export const apiPostEvolutionReapply = (threadId: string, reviewId: string) =>
  apiPost<EvolutionDecisionResponse>(
    `/api/threads/${threadId}/evolution/reviews/${encodeURIComponent(reviewId)}/reapply`,
  ).then((response) => ({
    ...response,
    review: normalizeEvolutionReview(response.review),
  }));

type ClaudeProjectsRefreshEvent =
  | {
      frame_type: "progress";
      current: number;
      total: number;
      current_project: string;
    }
  | {
      frame_type: "done";
      projects: ClaudeProjectSummaryDTO[];
    }
  | {
      frame_type: "error";
      detail: string;
    };

export async function apiRefreshClaudeProjects(
  onProgress: (progress: ClaudeProjectsRefreshProgressDTO) => void,
): Promise<{ projects: ClaudeProjectSummaryDTO[] }> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/api/claude/projects/refresh`, {
      method: "GET",
      headers: {
        "X-Requested-With": "XMLHttpRequest",
      },
      credentials: "include",
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new ApiError(0, "network", `网络错误：${msg}`);
  }

  if (response.status === 401) {
    await handle401();
    throw new ApiError(401, "unauthenticated", "session expired");
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      "internal",
      response.statusText ?? "request failed",
    );
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new ApiError(0, "network", "刷新响应为空");
  }

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const event = JSON.parse(trimmed) as ClaudeProjectsRefreshEvent;
      if (event.frame_type === "progress") {
        onProgress(event);
        continue;
      }
      if (event.frame_type === "error") {
        throw new ApiError(500, "internal", event.detail);
      }
      return { projects: event.projects };
    }

    if (done) break;
  }

  throw new ApiError(0, "network", "刷新响应提前结束");
}
