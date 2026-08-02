/**
 * message-runtime-v0.1 · chat 运行时收发审计日志（#5）
 *
 * 普通发送、接收和会话控制保留完整结构化记录。高频 content/reasoning delta 只保留
 * 每 turn 的无正文摘要，避免 ring buffer 与 console 长时间持有大段重复字符串。
 *
 * 方向语义：
 * - `send`   浏览器 → 后端（用户发送、打断等出站动作的完整请求）
 * - `recv`   后端 → 浏览器（入站原始帧 + 翻译出的统一事件）
 * - `call`   一次能力调用的入口（loadHistory / checkSessionStatus 等）
 * - `result` 一次能力调用的返回
 */
export type ChatLogDirection = "send" | "recv" | "call" | "result";

export interface ChatLogEntry {
  /** 记录时间（ms epoch），与 ChatEvent.createdAt 同语义。 */
  ts: number;
  direction: ChatLogDirection;
  /** 事件名，形如 `sendMessage.out` / `ingestFrame.events`。 */
  event: string;
  /** 完整明细：请求体 / 原始帧 / 翻译后的事件等，不做裁剪。 */
  detail: Record<string, unknown>;
}

const MAX_BUFFER = 500;
const buffer: ChatLogEntry[] = [];
let consoleEnabled = true;

interface DeltaSummary {
  readonly threadId: string;
  readonly runId: string;
  readonly turnId: string;
  firstAt: number;
  lastAt: number;
  eventCount: number;
  contentChars: number;
  reasoningChars: number;
  timer: ReturnType<typeof globalThis.setTimeout> | null;
}

const deltaSummaries = new Map<string, DeltaSummary>();
let deltaSummaryIntervalMs = 1000;

/** 记一条 chat 收发审计日志。 */
export function logChat(
  direction: ChatLogDirection,
  event: string,
  detail: Record<string, unknown>,
): void {
  const entry: ChatLogEntry = { ts: Date.now(), direction, event, detail };
  buffer.push(entry);
  if (buffer.length > MAX_BUFFER) buffer.shift();
  if (consoleEnabled) {
    // 统一前缀便于在 devtools 过滤；detail 作为结构化对象保留完整字段。
    console.debug(`[chat][${direction}] ${event}`, detail);
  }
}

/**
 * 累积一条流式 delta 的元数据。正文只用于计算字符数，绝不进入审计对象、ring buffer
 * 或 console；interval / terminal / stream-error / dispose 都会输出最后摘要。
 */
export function logChatDelta(input: {
  threadId: string;
  runId?: string;
  turnId: string;
  content?: string;
  reasoning?: string;
}): void {
  const key = deltaKey(input.threadId, input.runId ?? "", input.turnId);
  const now = Date.now();
  let summary = deltaSummaries.get(key);
  if (!summary) {
    summary = {
      threadId: input.threadId,
      runId: input.runId ?? "",
      turnId: input.turnId,
      firstAt: now,
      lastAt: now,
      eventCount: 0,
      contentChars: 0,
      reasoningChars: 0,
      timer: null,
    };
    deltaSummaries.set(key, summary);
    summary.timer = globalThis.setTimeout(() => flushDeltaSummary(key, "interval"), deltaSummaryIntervalMs);
  }
  summary.eventCount += 1;
  summary.contentChars += input.content?.length ?? 0;
  summary.reasoningChars += input.reasoning?.length ?? 0;
  summary.lastAt = now;
}

/** 在 terminal 或流失败边界立即写出当前 turn 的剩余摘要。 */
export function flushChatDeltaLog(threadId: string, runId: string | undefined, turnId: string, reason: "terminal" | "stream-error"): void {
  const exactKey = deltaKey(threadId, runId ?? "", turnId);
  if (deltaSummaries.has(exactKey)) {
    flushDeltaSummary(exactKey, reason);
    return;
  }
  // generic `error` wire 不带 run_id，Store 也以 activeStreamingTurnId 收口。
  // 日志沿用同一事实：只在 stream-error 下回退当前 thread 最近的未结摘要。
  if (reason !== "stream-error") return;
  const fallbackKey = [...deltaSummaries.entries()]
    .reverse()
    .find(([, summary]) => summary.threadId === threadId)?.[0];
  if (fallbackKey) flushDeltaSummary(fallbackKey, reason);
}

/** 释放 Store 时收口该 thread 的摘要 timer。 */
export function disposeChatDeltaLogs(threadId: string): void {
  for (const [key, summary] of deltaSummaries) {
    if (summary.threadId === threadId) flushDeltaSummary(key, "dispose");
  }
}

/** 测试专用：覆盖摘要周期，恢复函数保证全局状态不泄漏。 */
export function __setChatLogPolicyForTest(intervalMs: number): () => void {
  const previous = deltaSummaryIntervalMs;
  deltaSummaryIntervalMs = intervalMs;
  return () => {
    deltaSummaryIntervalMs = previous;
  };
}

function flushDeltaSummary(key: string, reason: "interval" | "terminal" | "stream-error" | "dispose"): void {
  const summary = deltaSummaries.get(key);
  if (!summary) return;
  deltaSummaries.delete(key);
  if (summary.timer) globalThis.clearTimeout(summary.timer);
  logChat("recv", "ingestFrame.delta_summary", {
    threadId: summary.threadId,
    runId: summary.runId,
    turnId: summary.turnId,
    eventCount: summary.eventCount,
    contentChars: summary.contentChars,
    reasoningChars: summary.reasoningChars,
    firstAt: summary.firstAt,
    lastAt: summary.lastAt,
    reason,
  });
}

function deltaKey(threadId: string, runId: string, turnId: string): string {
  return `${threadId}\u0000${runId}\u0000${turnId}`;
}

/** 导出当前缓冲的全部审计日志（最多 MAX_BUFFER 条），供排障 / 复盘。 */
export function getChatLog(): ChatLogEntry[] {
  return [...buffer];
}

/** 清空审计缓冲。 */
export function clearChatLog(): void {
  for (const summary of deltaSummaries.values()) {
    if (summary.timer) globalThis.clearTimeout(summary.timer);
  }
  deltaSummaries.clear();
  buffer.length = 0;
}

/** 开关 console 输出（不影响 ring buffer）；测试可关闭以免刷屏。 */
export function setChatConsoleLog(enabled: boolean): void {
  consoleEnabled = enabled;
}
