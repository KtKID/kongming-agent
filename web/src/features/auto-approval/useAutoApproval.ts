import { create } from "zustand";
import type {
  AutoApprovalQueryFrame,
  AutoApprovalStateFrame,
  AutoApprovalSetModeFrame,
} from "@/protocol";

/**
 * smart-approval-v1 per-cwd 状态 store。
 *
 * 状态来源：后端 `auto_approval_state` 帧（连接建立时主动 push / set-mode 回执 / query 应答）。
 * UI 操作：模式选择走 `auto-approval-set-mode` 帧，回执后端会再发一次 state 自动刷新。
 *
 * 不缓存计算结果，每次 toggle/query 都走 WS round-trip → 后端是真源。
 *
 * 多 cwd 共存（用户跨 project 切换 thread）：每个 cwd 独立 key。
 */

/** 单 cwd 的状态快照（与后端 AutoApprovalStateFrame 对齐，去掉 kind/channel） */
export interface AutoApprovalCwdState {
  mode: "user" | "llm" | "full_trust";
  timeoutMs: number;
  ruleOverrides: Record<string, boolean>;
}

interface AutoApprovalStore {
  /** key = cwd */
  byCwd: Record<string, AutoApprovalCwdState>;
  /** 接收 WS 帧 → 更新 store */
  applyStateFrame: (frame: AutoApprovalStateFrame) => void;
  /** 清空（断线 / 切 thread 时不必清；store 是用户视角 per-cwd 的稳定状态） */
  clear: () => void;
}

/** 极简的"能发送指定帧"的 socket 接口（避免引入完整 ClaudeCodeSocket 类型） */
export interface AutoApprovalSocket {
  send: (frame: AutoApprovalSetModeFrame | AutoApprovalQueryFrame) => boolean | void;
}

export const useAutoApprovalStore = create<AutoApprovalStore>((set) => ({
  byCwd: {},
  applyStateFrame: (frame) =>
    set((s) => ({
      byCwd: {
        ...s.byCwd,
        [frame.cwd]: {
          mode: frame.mode,
          timeoutMs: frame.timeoutMs,
          ruleOverrides: { ...frame.ruleOverrides },
        },
      },
    })),
  clear: () => set({ byCwd: {} }),
}));

/** 选择器：取指定 cwd 的状态；未拿到时返回 null（UI 应保守渲染为关闭态） */
export function selectCwdState(cwd: string | undefined | null) {
  return (s: AutoApprovalStore): AutoApprovalCwdState | null =>
    cwd ? (s.byCwd[cwd] ?? null) : null;
}

/**
 * 主动查询 cwd 的状态。
 *
 * 用例：进入新 thread 页面、socket 重连后；后端会立即回一条 `auto_approval_state`。
 * 调用方应捕获 send 的潜在异常（socket 未连接），本函数不抛。
 */
export function queryAutoApproval(socket: AutoApprovalSocket, cwd: string): void {
  if (!cwd) return;
  try {
    socket.send({ frame_type: "auto-approval-query", cwd });
  } catch {
    // socket 未连接 / 已关闭——静默；下次 socket open 时调用方会再触发 query
  }
}

/**
 * 设置处置模式：发送 set-mode 帧；后端会回 state 帧自动刷新 store。
 *
 * 同时 optimistic 更新本地 store（避免按下后立刻收到 state 之间的 UI 闪烁）。
 * 若 send 抛错（socket 断），不 optimistic、调用方应感知失败。
 */
export function setAutoApprovalMode(
  socket: AutoApprovalSocket,
  cwd: string,
  mode: "user" | "llm" | "full_trust",
): void {
  if (!cwd) return;
  try {
    const sent = socket.send({ frame_type: "auto-approval-set-mode", cwd, mode });
    if (sent === false) return;
  } catch {
    // socket 不可用：交给上层 UI 反馈；不 optimistic
    return;
  }
  // optimistic 更新（其他字段保持原值；后端 state 帧回来会全量刷新）
  const s = useAutoApprovalStore.getState();
  const prev = s.byCwd[cwd];
  useAutoApprovalStore.setState({
    byCwd: {
      ...s.byCwd,
      [cwd]: {
        mode,
        timeoutMs: prev?.timeoutMs ?? 10000,
        ruleOverrides: prev?.ruleOverrides ?? {},
      },
    },
  });
}
