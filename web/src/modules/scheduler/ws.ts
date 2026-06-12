/**
 * Scheduler 模块 WebSocket 封装
 *
 * 只封装 /ws/cron 端点：连接、断开、消息解析、事件回调注册。
 *
 * v0.2 加固（reports/cr/cr-report-20260519-web-crash-investigation.md P1 #2）：
 * - 加 MAX_RETRY=10 + 指数退避 1s → 30s（参考 useThreadStatusWS）
 * - close.code === 1008（POLICY_VIOLATION / 鉴权失败）→ 立刻停止重连
 *   避免未登录页 / 过期 cookie 触发的"每 3s 重连"风暴
 *   （实测 5/18 server.log 出现 19359 次 `/ws/cron 403`，全因这一无退避漏洞）
 * - 暴露 `_resetForTesting()` 给单测复位 module-level 状态
 */

const WS_BASE = (import.meta.env.VITE_WS_BASE as string | undefined) ?? "";

const MAX_RETRY = 10;
const MAX_BACKOFF_MS = 30_000;
const WS_CLOSE_POLICY_VIOLATION = 1008;

export type CronWSMessage = {
  frame_type: string;
  [key: string]: unknown;
};

export type OnCronWSMessage = (msg: CronWSMessage) => void;

let ws: WebSocket | null = null;
let onMessageCallback: OnCronWSMessage | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let retryCount = 0;
let givenUp = false;

export function connectCronWS(onMessage: OnCronWSMessage): void {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  if (givenUp) return; // policy violation / max retries 后放弃

  onMessageCallback = onMessage;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}${WS_BASE}/ws/cron`;

  ws = new WebSocket(url);

  ws.onopen = () => {
    // 连接成功 → 重置 retry 计数
    retryCount = 0;
  };

  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data as string) as CronWSMessage;
      onMessageCallback?.(data);
    } catch {
      // 忽略非 JSON 消息
    }
  };

  ws.onclose = (ev) => {
    ws = null;

    // 鉴权失败（policy violation）→ 立即放弃，不重连
    // 后端 cron_ws.py 在 cookie 缺失 / 失效时 `ws.close(1008)`
    if (ev.code === WS_CLOSE_POLICY_VIOLATION) {
      givenUp = true;
      console.warn(
        "[cronWS] policy violation (1008); auth failed, not reconnecting",
      );
      return;
    }

    // 超过 MAX_RETRY → 放弃
    if (retryCount >= MAX_RETRY) {
      givenUp = true;
      console.warn(
        `[cronWS] max retries (${MAX_RETRY}) exceeded; giving up`,
      );
      return;
    }

    // 指数退避：1s → 2s → 4s → ... → 30s 封顶
    const delay = Math.min(1000 * 2 ** retryCount, MAX_BACKOFF_MS);
    retryCount += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (onMessageCallback && !givenUp) connectCronWS(onMessageCallback);
    }, delay);
  };

  ws.onerror = () => {
    ws?.close();
  };
}

export function disconnectCronWS(): void {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
  onMessageCallback = null;
  // disconnect 是显式调用（如组件 unmount）→ 重置 retry 状态，让下次 connect 干净开始
  retryCount = 0;
  givenUp = false;
}

/**
 * 仅测试用：复位所有 module-level 状态，避免用例间互相污染。
 *
 * 不导出到 index——通过具名 import 引用即可。
 */
export function _resetForTesting(): void {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  ws = null;
  onMessageCallback = null;
  retryCount = 0;
  givenUp = false;
}
