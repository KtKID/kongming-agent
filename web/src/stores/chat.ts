import { create } from "zustand";
import type {
  ApprovalRequestFrame,
  AssistantFinalFrame,
  ErrorFrame,
  EvolutionReviewDTO,
  HistoryMessageDTO,
  SystemNoticeFrame,
  ThreadMetadataDTO,
  ToolCallEndFrame,
  ToolCallStartFrame,
  UsageFrame,
} from "@/protocol";

/**
 * kongming-agent v0.1.5 chat store + 流式 commit 节流
 *
 * ## 渲染单元（ChatItem）
 *
 * 一条消息列表项；UI 按 `kind` 分类渲染。
 * 流式 assistant 消息的 `content` / `reasoning` 由 module-level buffer 累积，
 * 每个 rAF 批量 setState 一次，避免高频 setState。
 *
 * ## buffer 设计
 *
 * `buffers: Map<turn_key, StreamingBuffer>`，turn_key = `${threadId}:${runId}:${turn}`。
 * 仅在 `turn.end` / `flushTurn()` 时把 buffer 内容 commit 到 store 并清 buffer。
 *
 * ### 为什么 key 含 runId（M4 修 bug）
 *
 * runner 每次 `run_once` 都新建 RunState，turn 从 1 重数。在仅靠 `(threadId, turn)`
 * 的旧设计里，连续两轮非流式对话的 turn=1 撞车 → 后一轮 content 覆盖前一轮的
 * ChatItem，前端看起来"没刷新"。引入 run_id（`run-{session_id}-{n}` 由后端
 * `session.advance_run_index` 自增）后，`(threadId, runId, turn)` 三参做 buffer
 * 与 ChatItem 的复合 key，确保不同 run 的同 turn 互相隔离。
 *
 * 旧后端不带 run_id 时 frame.run_id 为 undefined，本 store 兜底为空串 `""`，
 * 行为与旧版一致（同一 thread 内仍按 turn 唯一），不破坏现有 thread.history
 * 重放路径。
 */

export interface UsageSnapshot {
  lastPrompt: number;
  lastCompletion: number;
  lastTotal: number;
  cumulativePrompt: number;
  cumulativeCompletion: number;
  cumulativeTotal: number;
  cumulativeCacheRead: number | null;
  cumulativeCacheCreation: number | null;
}

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
      timestampMs: number;
    }
  | {
      id: string;
      kind: "approval";
      threadId: string;
      turn: number;
      callId: string;
      toolName: string;
      arguments: Record<string, unknown>;
      reason?: string;
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
  /** thread_id → 最近一次 usage 快照 */
  usageByThread: Record<string, UsageSnapshot>;
  setHistory: (threadId: string, history: HistoryMessageDTO[]) => void;
  appendUser: (threadId: string, content: string) => void;
  appendAssistantFinal: (threadId: string, frame: AssistantFinalFrame) => void;
  appendToolStart: (threadId: string, frame: ToolCallStartFrame) => void;
  appendToolEnd: (threadId: string, frame: ToolCallEndFrame) => void;
  appendApprovalRequest: (
    threadId: string,
    frame: ApprovalRequestFrame,
  ) => void;
  appendSystemNotice: (threadId: string, frame: SystemNoticeFrame) => void;
  applyEvolutionReview: (threadId: string, review: EvolutionReviewDTO) => void;
  appendError: (threadId: string, frame: ErrorFrame) => void;
  appendUsage: (threadId: string, frame: UsageFrame) => void;
  hydrateUsageFromThreads: (threads: ThreadMetadataDTO[]) => void;
  /** 仅供 hooks/tests 用：清空某 thread */
  clear: (threadId: string) => void;
}

interface StreamingBuffer {
  content: string;
  reasoning: string;
}

const buffers = new Map<string, StreamingBuffer>();
let rafScheduled = false;
let testRafImpl: ((cb: () => void) => void) | null = null;

/** 测试用：注入同步 rAF 模拟，便于断言 commit 行为。 */
export function __setRafForTest(impl: ((cb: () => void) => void) | null) {
  testRafImpl = impl;
}

function bufferKey(threadId: string, runId: string, turn: number): string {
  return `${threadId}:${runId}:${turn}`;
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

function scheduleCommit(): void {
  if (rafScheduled) return;
  rafScheduled = true;

  const runCommit = () => {
    rafScheduled = false;
    commitBuffersToStore();
  };

  if (testRafImpl) {
    testRafImpl(runCommit);
    return;
  }
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(runCommit);
  } else {
    setTimeout(runCommit, 16);
  }
}

function commitBuffersToStore(): void {
  if (buffers.size === 0) return;
  const snapshot = new Map(buffers);
  useChatStore.setState((s) => {
    const next: Record<string, ChatItem[]> = { ...s.itemsByThread };
    for (const [key, buf] of snapshot.entries()) {
      // key 形如 `${threadId}:${runId}:${turn}`，runId 可能为空串（旧后端兼容）
      const parts = key.split(":");
      if (parts.length < 3) continue;
      const threadId = parts[0]!;
      const turnStr = parts[parts.length - 1]!;
      const runId = parts.slice(1, -1).join(":");
      if (!threadId) continue;
      const turn = Number(turnStr);
      const list = next[threadId] ?? [];
      const idx = list.findIndex(
        (it) =>
          it.kind === "assistant" &&
          it.turn === turn &&
          it.runId === runId,
      );
      if (idx >= 0) {
        const old = list[idx];
        if (old && old.kind === "assistant") {
          const updated: ChatItem = {
            ...old,
            content: buf.content,
            reasoning: buf.reasoning,
          };
          next[threadId] = [
            ...list.slice(0, idx),
            updated,
            ...list.slice(idx + 1),
          ];
        }
      } else {
        // 该 (run, turn) 还没 placeholder：补一个 streaming assistant
        const newItem: ChatItem = {
          id: `assistant-${threadId}-${runId}-${turn}`,
          kind: "assistant",
          threadId,
          turn,
          runId,
          content: buf.content,
          reasoning: buf.reasoning,
          timestampMs: Date.now(),
          streaming: true,
        };
        next[threadId] = [...list, newItem];
      }
    }
    return { itemsByThread: next };
  });
}

/** 流式 delta：写 buffer，rAF 批量 commit */
export function appendDelta(
  threadId: string,
  kind: "content" | "reasoning",
  delta: string,
  turn: number,
  runId: string = "",
): void {
  const key = bufferKey(threadId, runId, turn);
  let b = buffers.get(key);
  if (!b) {
    b = { content: "", reasoning: "" };
    buffers.set(key, b);
  }
  if (kind === "content") b.content += delta;
  else b.reasoning += delta;
  scheduleCommit();
}

/** turn.end：强制 flush 一次 + 标记 streaming=false + 清 buffer */
export function flushTurn(
  threadId: string,
  turn: number,
  runId: string = "",
): void {
  const key = bufferKey(threadId, runId, turn);
  const b = buffers.get(key);
  if (b) {
    // 同步 commit 一次（不走 rAF）
    commitBuffersToStore();
    buffers.delete(key);
  }
  // 标记 streaming=false（按 (turn, runId) 复合定位）
  useChatStore.setState((s) => {
    const list = s.itemsByThread[threadId];
    if (!list) return s;
    const idx = list.findIndex(
      (it) =>
        it.kind === "assistant" && it.turn === turn && it.runId === runId,
    );
    if (idx < 0) return s;
    const old = list[idx];
    if (!old || old.kind !== "assistant") return s;
    const updated: ChatItem = { ...old, streaming: false };
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
}

/** 测试 / 切 thread 时清 buffer */
export function clearBuffers(threadId?: string): void {
  if (!threadId) {
    buffers.clear();
    return;
  }
  for (const key of Array.from(buffers.keys())) {
    if (key.startsWith(`${threadId}:`)) buffers.delete(key);
  }
}

export const useChatStore = create<ChatState>((set) => ({
  itemsByThread: {},
  usageByThread: {},

  setHistory: (threadId, history) => {
    const items: ChatItem[] = history.map((m, i) => {
      if (m.role === "user") {
        return {
          id: `hist-${threadId}-${i}`,
          kind: "user",
          threadId,
          content: m.content,
          timestampMs: m.timestamp_ms,
        };
      }
      if (m.role === "assistant") {
        return {
          id: `hist-${threadId}-${i}`,
          kind: "assistant",
          threadId,
          turn: m.turn,
          // 历史消息没有 run_id；用空串占位（不参与 buffer 匹配）
          runId: "",
          content: m.content,
          reasoning: "",
          timestampMs: m.timestamp_ms,
          streaming: false,
        };
      }
      // tool
      // v0.1.6: 历史重放路径补齐 toolName / result(content) / resultData /
      // errorMessage / ok。arguments 在 Message 上没存（在前面 assistant 的
      // tool_calls 里），暂留 {} 直至跨消息重建实现。
      return {
        id: `hist-${threadId}-${i}`,
        kind: "tool",
        threadId,
        turn: m.turn,
        runId: "",
        toolName: m.tool_name ?? "",
        callId: m.tool_call_id ?? `hist-${i}`,
        arguments: {},
        ok: m.ok ?? true,
        errorMessage: m.error_message ?? undefined,
        result: m.content,
        resultData: m.data ?? null,
        timestampMs: m.timestamp_ms,
      };
    });
    set((s) => ({
      itemsByThread: { ...s.itemsByThread, [threadId]: items },
    }));
  },

  appendUser: (threadId, content) => {
    const item: ChatItem = {
      id: `user-${threadId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind: "user",
      threadId,
      content,
      timestampMs: Date.now(),
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

  appendApprovalRequest: (threadId, frame) => {
    const item: ChatItem = {
      id: `approval-${threadId}-${frame.call_id}`,
      kind: "approval",
      threadId,
      turn: frame.turn,
      callId: frame.call_id,
      toolName: frame.tool_name,
      arguments: frame.arguments,
      reason: frame.reason,
      timestampMs: frame.timestamp_ms,
    };
    set((s) => ({
      itemsByThread: {
        ...s.itemsByThread,
        [threadId]: [...(s.itemsByThread[threadId] ?? []), item],
      },
    }));
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
    set((s) => {
      const prev = s.usageByThread[threadId];
      const runId = frame.run_id ?? "";
      const list = s.itemsByThread[threadId] ?? [];
      const idx = list.findIndex(
        (it) =>
          it.kind === "assistant" &&
          it.turn === frame.turn &&
          it.runId === runId,
      );
      const nextItemsByThread =
        idx >= 0
          ? {
              ...s.itemsByThread,
              [threadId]: [
                ...list.slice(0, idx),
                {
                  ...list[idx]!,
                  usage: {
                    prompt: frame.prompt_tokens,
                    completion: frame.completion_tokens,
                    total: frame.total_tokens,
                  },
                },
                ...list.slice(idx + 1),
              ],
            }
          : s.itemsByThread;
      return {
        itemsByThread: nextItemsByThread,
        usageByThread: {
          ...s.usageByThread,
          [threadId]: {
            lastPrompt: frame.prompt_tokens,
            lastCompletion: frame.completion_tokens,
            lastTotal: frame.total_tokens,
            cumulativePrompt: (prev?.cumulativePrompt ?? 0) + frame.prompt_tokens,
            cumulativeCompletion: (prev?.cumulativeCompletion ?? 0) + frame.completion_tokens,
            cumulativeTotal: (prev?.cumulativeTotal ?? 0) + frame.total_tokens,
            cumulativeCacheRead: frame.cache_read_tokens != null
              ? (prev?.cumulativeCacheRead ?? 0) + frame.cache_read_tokens
              : (prev?.cumulativeCacheRead ?? null),
            cumulativeCacheCreation: frame.cache_creation_tokens != null
              ? (prev?.cumulativeCacheCreation ?? 0) + frame.cache_creation_tokens
              : (prev?.cumulativeCacheCreation ?? null),
          },
        },
      };
    });
  },

  hydrateUsageFromThreads: (threads) => {
    set((s) => {
      const nextUsage = { ...s.usageByThread };
      for (const thread of threads) {
        const prev = nextUsage[thread.id];
        nextUsage[thread.id] = {
          lastPrompt: prev?.lastPrompt ?? 0,
          lastCompletion: prev?.lastCompletion ?? 0,
          lastTotal: prev?.lastTotal ?? 0,
          cumulativePrompt: Math.max(
            prev?.cumulativePrompt ?? 0,
            thread.cumulative_prompt_tokens ?? 0,
          ),
          cumulativeCompletion: Math.max(
            prev?.cumulativeCompletion ?? 0,
            thread.cumulative_completion_tokens ?? 0,
          ),
          cumulativeTotal: Math.max(
            prev?.cumulativeTotal ?? 0,
            thread.cumulative_total_tokens ?? 0,
          ),
          cumulativeCacheRead:
            thread.cumulative_cache_read_tokens != null
              ? Math.max(prev?.cumulativeCacheRead ?? 0, thread.cumulative_cache_read_tokens)
              : (prev?.cumulativeCacheRead ?? null),
          cumulativeCacheCreation:
            thread.cumulative_cache_creation_tokens != null
              ? Math.max(prev?.cumulativeCacheCreation ?? 0, thread.cumulative_cache_creation_tokens)
              : (prev?.cumulativeCacheCreation ?? null),
        };
      }
      return { usageByThread: nextUsage };
    });
  },

  clear: (threadId) => {
    set((s) => {
      const next = { ...s.itemsByThread };
      const nextUsage = { ...s.usageByThread };
      delete next[threadId];
      delete nextUsage[threadId];
      return { itemsByThread: next, usageByThread: nextUsage };
    });
    clearBuffers(threadId);
  },
}));
