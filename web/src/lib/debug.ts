/**
 * Debug 模式开关。
 *
 * 两种触发方式（任一命中即开）：
 * 1. URL query 参数 `?debug=1`
 * 2. localStorage `kongming.debug=1`
 *
 * 用途：MessageList 在 debug 开启时给每条 ChatItem 角上贴 monospace 小 badge，
 * 显示 `[id │ runId │ turn]`，方便肉眼验证 (threadId, runId, turn) 复合 key
 * 是否正确生成（即修非流式多轮覆盖 bug 的快速回归手段）。
 *
 * 设计要点：
 * - 同步读取，不响应式：首次组件渲染时读一次即可，无需监听 storage 事件
 * - SSR 安全：项目是 SPA 不会 SSR，但 `typeof window === "undefined"` 兜底
 * - localStorage 访问可能在隐私模式下抛错，try/catch 兜底
 */

export function isDebugMode(): boolean {
  if (typeof window === "undefined") return false;

  try {
    const url = new URLSearchParams(window.location.search);
    if (url.get("debug") === "1") return true;
  } catch {
    // URLSearchParams 不应抛，但保险起见兜底
  }

  try {
    return window.localStorage.getItem("kongming.debug") === "1";
  } catch {
    return false;
  }
}
