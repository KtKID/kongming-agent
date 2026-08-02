import { create } from "zustand";
import { apiGet, ApiError } from "@/lib/api";
import type {
  AssistantFinalFrame,
  ErrorFrame,
  EvolutionReviewDTO,
  NormalizedMessage,
  SystemNoticeFrame,
  ThreadUsage,
  ToolCallEndFrame,
  ToolCallStartFrame,
  UsageFrame,
  ConversationReferenceDTO,
  UserInputAttachment,
} from "@/protocol";

/**
 * kongming-agent v0.1.5 chat 展示兼容 store
 *
 * ## 渲染单元（ChatItem）
 *
 * 一条消息列表项；UI 按 `kind` 分类渲染。
 * 流式消息的运行时状态由 `ChatTimelineStore` 统一拥有。这里保留历史兼容、usage、
 * StatusLine 和展示类型，避免第二条 buffer / scheduler / visibility 生命周期旁路。
 */

// usage-token-v2-bigbang: v2 manager 是无状态门面，前端 store 直接持有
// 当前最新的 ThreadUsage DTO（自带 provider discriminator）。
// 不再有 v1 时代的 summary / lastSnapshot 分离视图——后端永远返回最新派生结果。

export interface AssistantUsage {
  prompt: number;
  completion: number;
  total: number;
}

export type SystemChatStatus = "running" | "success" | "warning" | "error";
export type SystemChatIcon = SystemChatStatus;

export type ChatItem =
  | {
      id: string;
      kind: "user";
      threadId: string;
      content: string;
      timestampMs: number;
      /** 用户消息附件（v0.1.5 多模态）；历史回显与实时 append 两条路径共用。 */
      attachments?: UserInputAttachment[];
      /** Slash catalog 选择生成的对话引用；历史回显与实时 append 两条路径共用。 */
      references?: ConversationReferenceDTO[];
      /** 立即发送已插入当前活跃 run 时的可见状态。 */
      deliveryStatus?: "steered";
    }
  | {
      id: string;
      kind: "assistant";
      threadId: string;
      turn: number;
      /** session 内自增的 run 编号；空串表示历史消息 / 旧后端 frame 无 run_id。 */
      runId: string;
      content: string;
      reasoning: string;
      usage?: AssistantUsage;
      /** 可 fork assistant 在 Session.history() 中的服务端零基位置。 */
      forkHistoryIndex?: number;
      timestampMs: number;
      streaming: boolean;
    }
  | {
      id: string;
      kind: "tool";
      threadId: string;
      turn: number;
      runId: string;
      toolName: string;
      callId: string;
      arguments: Record<string, unknown>;
      ok: boolean | null; // null = 进行中
      errorMessage?: string;
      /** 工具产出文本（ToolResult.content）；undefined = 还未结束 / 空输出。 */
      result?: string;
      /** 工具产出结构化数据（ToolResult.data）；undefined = 还未结束 / 无结构化输出。 */
      resultData?: Record<string, unknown> | null;
      /** Claude 流式 tool_use 期间累积的原始 input JSON 文本；pending=false 后清空。 */
      partialInput?: string;
      /** true=参数构建中（input_json delta 流式累积），false=已收到完整 tool_use 帧（区分 ok=null 的 running 子态）。 */
      pending?: boolean;
      timestampMs: number;
    }
  | {
      id: string;
      kind: "system";
      threadId: string;
      runId: string;
      noticeKey: string;
      source: string;
      title: string;
      message: string;
      details: string[];
      detailsData?: Record<string, unknown> | string[] | null;
      status: SystemChatStatus;
      icon: SystemChatIcon;
      timestampMs: number;
    }
  | {
      id: string;
      kind: "error";
      threadId: string;
      message: string;
      errorCode: string;
      timestampMs: number;
    };

interface ChatState {
  /** thread_id → items 列表 */
  itemsByThread: Record<string, ChatItem[]>;
  /** thread_id → 最新 token usage DTO（v2 派生结果，自带 provider discriminator） */
  usageByThread: Record<string, ThreadUsage>;
  setHistory: (threadId: string, history: NormalizedMessage[]) => void;
  appendUser: (
    threadId: string,
    content: string,
    attachments?: UserInputAttachment[],
  ) => void;
  appendAssistantFinal: (threadId: string, frame: AssistantFinalFrame) => void;
  appendToolStart: (threadId: string, frame: ToolCallStartFrame) => void;
  appendToolEnd: (threadId: string, frame: ToolCallEndFrame) => void;
  appendSystemNotice: (threadId: string, frame: SystemNoticeFrame) => void;
  applyEvolutionReview: (threadId: string, review: EvolutionReviewDTO) => void;
  appendError: (threadId: string, frame: ErrorFrame) => void;
  appendUsage: (threadId: string, frame: UsageFrame) => void;
  /**
   * 主动拉某 thread 当前 token 用量（usage-token-v2-ui-fields）。
   *
   * v2 后端通过独立端点 ``GET /api/threads/<tid>/usage`` 派生最新 usage DTO。
   * 用于打开 thread 时初始化 ``usageByThread[threadId]``，避免重启 web server 后
   * 旧 thread 没新对话时底栏一直显示「等待对话」。
   *
   * - 成功且 ``usage != null`` → 写入 ``usageByThread[threadId]``
   * - 404 / 网络失败 / usage=null → 静默忽略（等 WS UsageFrame 推送）
   */
  fetchThreadUsage: (threadId: string) => Promise<void>;
  /** 仅供 hooks/tests 用：清空某 thread */
  clear: (threadId: string) => void;
}

function normalizeSystemNoticeStatus(
  status: SystemNoticeFrame["status"],
): SystemChatStatus {
  switch (status) {
    case "started":
      return "running";
    case "completed":
      return "success";
    case "failed":
      return "error";
    case "drain_timeout":
      return "warning";
    default: {
      const _exhaustive: never = status;
      return _exhaustive;
    }
  }
}

function normalizeSystemNoticeIcon(
  icon: SystemNoticeFrame["icon"],
  status: SystemChatStatus,
): SystemChatIcon {
  switch (icon) {
    case "running":
    case "success":
    case "warning":
    case "error":
      return icon;
    default:
      return status;
  }
}

function normalizeSystemNoticeDetails(
  details: SystemNoticeFrame["details"],
): string[] {
  if (Array.isArray(details)) {
    return details.map((detail) => String(detail));
  }
  if (details && typeof details === "object") {
    return Object.entries(details).map(([key, value]) => {
      if (Array.isArray(value)) {
        return `${key}: ${value.map((item) => String(item)).join(", ")}`;
      }
      if (value && typeof value === "object") {
        return `${key}: ${JSON.stringify(value)}`;
      }
      return `${key}: ${String(value)}`;
    });
  }
  return [];
}

interface EvolutionApplyProgress {
  written: number;
  skipped: number;
  failed: number;
  pending: number;
  ignored: number;
  actionable: number;
}

function getEvolutionApplyProgress(
  review: EvolutionReviewDTO,
): EvolutionApplyProgress {
  let written = 0;
  let skipped = 0;
  let failed = 0;
  let pending = 0;
  let ignored = 0;
  let actionable = 0;

  for (const decision of review.decisions) {
    if (decision.decision === "ignore") {
      ignored += 1;
      continue;
    }
    actionable += 1;
    switch (decision.applied_status) {
      case "written":
        written += 1;
        break;
      case "skipped":
        skipped += 1;
        break;
      case "failed":
        failed += 1;
        break;
      case "pending":
      case null:
      case undefined:
        pending += 1;
        break;
      default:
        pending += 1;
        break;
    }
  }

  return {
    written,
    skipped,
    failed,
    pending,
    ignored,
    actionable,
  };
}

function messageForEvolutionReview(review: EvolutionReviewDTO): string {
  const total = review.decision_summary.total;
  const handled =
    review.decision_summary.accepted_memory +
    review.decision_summary.accepted_skill +
    review.decision_summary.ignored;
  const progress = getEvolutionApplyProgress(review);

  if (progress.actionable > 0) {
    const parts = [`已写入 ${progress.written}/${total} 条进化养料`];
    if (progress.skipped > 0) {
      parts.push(`已命中 ${progress.skipped} 条`);
    }
    if (progress.failed > 0) {
      parts.push(`失败 ${progress.failed} 条`);
    }
    if (progress.pending > 0) {
      parts.push(`待写入 ${progress.pending} 条`);
    }
    if (progress.ignored > 0) {
      parts.push(`已忽略 ${progress.ignored} 条`);
    }
    return parts.join("，");
  }

  if (handled > 0) {
    return `已处理 ${handled}/${total} 条进化养料`;
  }
  return `发现 ${total} 条进化养料`;
}

function detailsDataForEvolutionReview(
  review: EvolutionReviewDTO,
): Record<string, unknown> {
  const progress = getEvolutionApplyProgress(review);
  return {
    review_id: review.review_id,
    review_run_id: review.run_id,
    session_id: review.session_id,
    write_status: "written",
    nutrient_count: review.decision_summary.total,
    handled_count:
      review.decision_summary.accepted_memory +
      review.decision_summary.accepted_skill +
      review.decision_summary.ignored,
    pending_count: review.decision_summary.pending,
    accepted_memory_count: review.decision_summary.accepted_memory,
    accepted_skill_count: review.decision_summary.accepted_skill,
    ignored_count: review.decision_summary.ignored,
    applied_written_count: progress.written,
    applied_skipped_count: progress.skipped,
    applied_failed_count: progress.failed,
    applied_pending_count: progress.pending,
    ignored_decision_count: progress.ignored,
  };
}

function detailsForEvolutionReview(review: EvolutionReviewDTO): string[] {
  const progress = getEvolutionApplyProgress(review);
  const details = [
    `pending_count: ${review.decision_summary.pending}`,
    `accepted_memory_count: ${review.decision_summary.accepted_memory}`,
    `accepted_skill_count: ${review.decision_summary.accepted_skill}`,
    `ignored_count: ${review.decision_summary.ignored}`,
  ];

  if (progress.actionable > 0) {
    details.push(`applied_written_count: ${progress.written}`);
    details.push(`applied_skipped_count: ${progress.skipped}`);
    details.push(`applied_failed_count: ${progress.failed}`);
    details.push(`applied_pending_count: ${progress.pending}`);
  }

  return details;
}

export const useChatStore = create<ChatState>((set) => ({
  itemsByThread: {},
  usageByThread: {},

  setHistory: (threadId, history) => {
    const items: ChatItem[] = history.map((m, i) => {
      const parsedAt = m.timestamp ? Date.parse(m.timestamp) : Date.now();
      const timestampMs = Number.isNaN(parsedAt) ? Date.now() : parsedAt;
      if (m.frame_type === "text" && m.role === "user") {
        return {
          id: m.id ?? `hist-${threadId}-${i}`,
          kind: "user",
          threadId,
          content: String(m.content ?? ""),
          timestampMs,
        };
      }
      if (m.frame_type === "text") {
        return {
          id: m.id ?? `hist-${threadId}-${i}`,
          kind: "assistant",
          threadId,
          turn: i,
          runId: "",
          content: String(m.content ?? ""),
          reasoning: "",
          timestampMs,
          streaming: false,
        };
      }
      return {
        id: m.id ?? `hist-${threadId}-${i}`,
        kind: "tool",
        threadId,
        turn: i,
        runId: "",
        toolName: m.toolName ?? "",
        callId: m.toolId ?? `hist-${i}`,
        arguments:
          m.toolInput && typeof m.toolInput === "object"
            ? (m.toolInput as Record<string, unknown>)
            : {},
        ok: m.frame_type === "tool_use" ? null : m.isError === true ? false : true,
        errorMessage:
          m.frame_type === "tool_result" && m.isError === true
            ? String(m.content ?? "")
            : undefined,
        result: m.frame_type === "tool_result" ? String(m.content ?? "") : undefined,
        resultData: null,
        timestampMs,
      };
    });
    set((s) => ({
      itemsByThread: { ...s.itemsByThread, [threadId]: items },
    }));
  },

  appendUser: (threadId, content, attachments) => {
    const item: ChatItem = {
      id: `user-${threadId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind: "user",
      threadId,
      content,
      timestampMs: Date.now(),
      attachments,
    };
    set((s) => ({
      itemsByThread: {
        ...s.itemsByThread,
        [threadId]: [...(s.itemsByThread[threadId] ?? []), item],
      },
    }));
  },

  appendAssistantFinal: (threadId, frame) => {
    const runId = frame.run_id ?? "";
    set((s) => {
      const list = s.itemsByThread[threadId] ?? [];
      const idx = list.findIndex(
        (it) =>
          it.kind === "assistant" &&
          it.turn === frame.turn &&
          it.runId === runId,
      );
      if (idx >= 0) {
        const old = list[idx];
        if (!old || old.kind !== "assistant") return s;
        const updated: ChatItem = {
          ...old,
          content: frame.content,
          streaming: false,
        };
        return {
          itemsByThread: {
            ...s.itemsByThread,
            [threadId]: [
              ...list.slice(0, idx),
              updated,
              ...list.slice(idx + 1),
            ],
          },
        };
      }
      const item: ChatItem = {
        id: `assistant-${threadId}-${runId}-${frame.turn}`,
        kind: "assistant",
        threadId,
        turn: frame.turn,
        runId,
        content: frame.content,
        reasoning: "",
        timestampMs: frame.timestamp_ms,
        streaming: false,
      };
      return {
        itemsByThread: {
          ...s.itemsByThread,
          [threadId]: [...list, item],
        },
      };
    });
  },

  appendToolStart: (threadId, frame) => {
    const item: ChatItem = {
      id: `tool-${threadId}-${frame.call_id}`,
      kind: "tool",
      threadId,
      turn: frame.turn,
      runId: frame.run_id ?? "",
      toolName: frame.tool_name,
      callId: frame.call_id,
      arguments: frame.arguments,
      ok: null,
      timestampMs: frame.timestamp_ms,
    };
    set((s) => ({
      itemsByThread: {
        ...s.itemsByThread,
        [threadId]: [...(s.itemsByThread[threadId] ?? []), item],
      },
    }));
  },

  appendToolEnd: (threadId, frame) => {
    set((s) => {
      const list = s.itemsByThread[threadId] ?? [];
      const idx = list.findIndex(
        (it) => it.kind === "tool" && it.callId === frame.call_id,
      );
      if (idx < 0) return s;
      const old = list[idx];
      if (!old || old.kind !== "tool") return s;
      const updated: ChatItem = {
        ...old,
        ok: frame.ok,
        errorMessage: frame.error_message ?? undefined,
        result: frame.content,
        resultData: frame.data ?? null,
      };
      return {
        itemsByThread: {
          ...s.itemsByThread,
          [threadId]: [
            ...list.slice(0, idx),
            updated,
            ...list.slice(idx + 1),
          ],
        },
      };
    });
  },

  appendSystemNotice: (threadId, frame) => {
    const runId = frame.run_id ?? "";
    const status = normalizeSystemNoticeStatus(frame.status);
    const item: Extract<ChatItem, { kind: "system" }> = {
      id: `system-${threadId}-${runId}-${frame.notice_key}`,
      kind: "system",
      threadId,
      runId,
      noticeKey: frame.notice_key,
      source: frame.source,
      title: frame.title,
      message: frame.message,
      details: normalizeSystemNoticeDetails(frame.details),
      detailsData:
        Array.isArray(frame.details) || (frame.details && typeof frame.details === "object")
          ? frame.details
          : null,
      status,
      icon: normalizeSystemNoticeIcon(frame.icon, status),
      timestampMs: frame.timestamp_ms,
    };
    set((s) => {
      const list = s.itemsByThread[threadId] ?? [];
      const idx = list.findIndex(
        (it) =>
          it.kind === "system" &&
          it.runId === runId &&
          it.noticeKey === frame.notice_key,
      );
      if (idx < 0) {
        return {
          itemsByThread: {
            ...s.itemsByThread,
            [threadId]: [...list, item],
          },
        };
      }
      return {
        itemsByThread: {
          ...s.itemsByThread,
          [threadId]: [
            ...list.slice(0, idx),
            item,
            ...list.slice(idx + 1),
          ],
        },
      };
    });
  },

  applyEvolutionReview: (threadId, review) => {
    const runId = review.run_id;
    const detailsData = detailsDataForEvolutionReview(review);
    const item: Extract<ChatItem, { kind: "system" }> = {
      id: `system-${threadId}-${runId}-self_evolution.review`,
      kind: "system",
      threadId,
      runId,
      noticeKey: "self_evolution.review",
      source: "self_evolution",
      title: "进化复盘",
      message: messageForEvolutionReview(review),
      details: detailsForEvolutionReview(review),
      detailsData,
      status: "success",
      icon: "success",
      timestampMs: review.reviewed_at_ms,
    };
    set((s) => {
      const list = s.itemsByThread[threadId] ?? [];
      const idx = list.findIndex(
        (it) =>
          it.kind === "system" &&
          it.runId === runId &&
          it.noticeKey === "self_evolution.review",
      );
      if (idx < 0) {
        return {
          itemsByThread: {
            ...s.itemsByThread,
            [threadId]: [...list, item],
          },
        };
      }
      return {
        itemsByThread: {
          ...s.itemsByThread,
          [threadId]: [
            ...list.slice(0, idx),
            item,
            ...list.slice(idx + 1),
          ],
        },
      };
    });
  },

  appendError: (threadId, frame) => {
    const item: ChatItem = {
      id: `error-${threadId}-${frame.timestamp_ms}-${Math.random().toString(36).slice(2, 6)}`,
      kind: "error",
      threadId,
      message: frame.message,
      errorCode: frame.error_code,
      timestampMs: frame.timestamp_ms,
    };
    set((s) => ({
      itemsByThread: {
        ...s.itemsByThread,
        [threadId]: [...(s.itemsByThread[threadId] ?? []), item],
      },
    }));
  },

  appendUsage: (threadId, frame) => {
    // task#3.3+#4：WS UsageFrame.snapshot 是 UsageTokenSnapshot 嵌套结构。
    // 把 snapshot 存为 lastSnapshot；message 卡片上的 usage 字段从 snapshot 派生
    // usage-token-v2-bigbang: 直接把 UsageFrame.usage（ThreadUsage union DTO）
    // 写入 store；不再有 v1 平铺累加。message 卡片上的 prompt/completion 数字
    // 按 provider 分支提取：Claude 用 input/output，OpenAI 用 last.input/output。
    set((s) => {
      const usage: ThreadUsage = frame.usage;
      const runId = frame.run_id ?? "";
      const list = s.itemsByThread[threadId] ?? [];
      const idx = list.findIndex(
        (it) =>
          it.kind === "assistant" &&
          it.turn === frame.turn &&
          it.runId === runId,
      );
      // 按 provider 分支取 prompt / completion 数字
      let prompt: number | null;
      let completion: number | null;
      if (usage.provider === "claude") {
        prompt = usage.input_tokens;
        completion = usage.output_tokens;
      } else {
        // openai 系（CodexUsage 或 GenericChatOpenAIUsage 都有 last）
        prompt = usage.last.input_tokens;
        completion = usage.last.output_tokens;
      }
      const nextItemsByThread =
        idx >= 0
          ? {
              ...s.itemsByThread,
              [threadId]: [
                ...list.slice(0, idx),
                {
                  ...list[idx]!,
                  ...(prompt != null && completion != null
                    ? {
                        usage: {
                          prompt,
                          completion,
                          total: prompt + completion,
                        },
                      }
                    : {}),
                },
                ...list.slice(idx + 1),
              ],
            }
          : s.itemsByThread;
      return {
        itemsByThread: nextItemsByThread,
        usageByThread: {
          ...s.usageByThread,
          [threadId]: usage,
        },
      };
    });
  },

  fetchThreadUsage: async (threadId) => {
    // usage-token-v2-ui-fields: 取代 v1 时代 ThreadMetadataDTO 上 usage 字段；
    // 解决重启 web server 后旧 thread 底栏一直「等待对话」的问题。
    //
    // 端点返回 ``{"usage": ClaudeUsage | CodexUsage | GenericChat*Usage | null}``，
    // 自带 ``provider`` discriminator；usage=null 表示 thread 没绑 SDK 真源 / 派生失败。
    try {
      const body = await apiGet<{ usage: ThreadUsage | null }>(
        `/api/threads/${threadId}/usage`,
      );
      const usage = body?.usage;
      if (!usage) return;
      set((s) => ({
        usageByThread: {
          ...s.usageByThread,
          [threadId]: usage,
        },
      }));
    } catch (err) {
      // 404 / 网络失败：静默；WS UsageFrame 后续推送会兜底
      if (err instanceof ApiError) return;
      // 其它意外保持静默，避免红屏（底栏不是 critical path）
    }
  },

  clear: (threadId) => {
    set((s) => {
      const next = { ...s.itemsByThread };
      const nextUsage = { ...s.usageByThread };
      delete next[threadId];
      delete nextUsage[threadId];
      return { itemsByThread: next, usageByThread: nextUsage };
    });
  },
}));
