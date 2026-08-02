/**
 * chat-running-state-unify #1：三频道共用的「是否运行中」判断。
 *
 * 唯一真源 = 后端 `/ws/thread-status` 广播的 `ThreadStatusPhase`。
 * generic / claude / codex 三视图统一 import 本 hook，不再各自前端推导
 * ——前端推导的 streaming 标志会出现「对话结束后不可靠复位」导致停止按钮
 * 卡住的 bug（generic 既往现象，见 task README 风险段）。
 *
 * 终态兜底（run-end-reason-fix）：即便 phase 因竞态仍停在 running 集合内，
 * 只要收到过结束帧（run_end_reason > 0）就强制判定为「不在运行」，让停止按钮
 * 一定能复位。这是对 phase 判断的双保险。
 *
 * task：`dev-pipeline/tasks/chat-running-state-unify/README.md`
 */
import type { ThreadStatusPhase } from "@/protocol";
import { useThreadStatusStore } from "@/stores/threadStatus";

/**
 * 三频道共用的「运行中」phase 集合，唯一真源。
 *
 * `idle` / `complete` / `error` 不在集合内——会话结束 / 空闲 / 出错时
 * `useThreadRunning` 返回 `false`，停止按钮自动复位为发送。
 */
export const RUNNING_PHASES: ReadonlySet<ThreadStatusPhase> = new Set<ThreadStatusPhase>([
  "responding",
  "thinking",
  "tool_calling",
  "waiting_approval",
]);

/**
 * 判定指定 thread 是否在运行中。
 *
 * - `threadId === undefined`（未进入会话）→ `false`
 * - 该 thread 在 store 无 entry（尚未收到 thread-status 广播）→ `false`
 * - 收到过结束帧（`run_end_reason > 0`）→ `false`（终态兜底，优先于 phase）
 * - phase 不在 `RUNNING_PHASES` 内（idle / complete / error）→ `false`
 * - phase 在 `RUNNING_PHASES` 内 → `true`
 */
export function useThreadRunning(threadId: string | undefined): boolean {
  const entry = useThreadStatusStore((s) =>
    threadId ? s.statuses[threadId] : undefined,
  );
  if (entry === undefined) {
    return false;
  }
  // 终态兜底：收到过结束帧就一定不在运行，让停止按钮复位。
  if (
    typeof entry.run_end_reason === "number" &&
    entry.run_end_reason > 0
  ) {
    return false;
  }
  return RUNNING_PHASES.has(entry.phase);
}
