/**
 * message-runtime-v0.1 · chat 运行时收发审计日志（#5）
 *
 * 审计为王：每一次发送 / 接收 / 会话控制都留**完整结构化记录**，验证与排障靠日志
 * 复盘真实收发，而不是猜代码行为。日志同时落 ring buffer（可在 devtools 用
 * `getChatLog()` 导出）与 console。
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

/** 导出当前缓冲的全部审计日志（最多 MAX_BUFFER 条），供排障 / 复盘。 */
export function getChatLog(): ChatLogEntry[] {
  return [...buffer];
}

/** 清空审计缓冲。 */
export function clearChatLog(): void {
  buffer.length = 0;
}

/** 开关 console 输出（不影响 ring buffer）；测试可关闭以免刷屏。 */
export function setChatConsoleLog(enabled: boolean): void {
  consoleEnabled = enabled;
}
