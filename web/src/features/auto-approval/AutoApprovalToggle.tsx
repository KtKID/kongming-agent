import { useEffect } from "react";
import {
  type AutoApprovalSocket,
  queryAutoApproval,
  selectCwdState,
  setAutoApprovalMode,
  useAutoApprovalStore,
} from "./useAutoApproval";

/**
 * smart-approval-v1 开关（per-cwd）。
 *
 * 渲染要点：
 * - 关闭态 (default 新 project)：图标 ZapOff + 灰；label "智能审批 关"
 * - 开启态：图标 Zap（高亮）+ label "智能审批 N s 自动通过"（N 来自 store.timeoutMs）
 * - tooltip 显示当前 cwd 简称（保留 host 维度的 hint，但 v1 用 native title 即可）
 *
 * 行为：
 * - 挂载 / cwd 切换 → 自动 query 一次后端最新状态
 * - 用户 toggle → 调 toggleAutoApproval（optimistic + 发 WS 帧 → 后端回 state 帧覆盖）
 * - 若没传 socket（未连上 / generic_chat 通道不支持）→ 不渲染（保守不干扰原 UI）
 */
interface Props {
  cwd?: string;
  socket?: AutoApprovalSocket | null;
}

export function AutoApprovalModeSelector({ cwd, socket }: Props) {
  const state = useAutoApprovalStore(selectCwdState(cwd));

  // 挂载 / cwd 变更 / socket 变更 → 主动 query 一次
  useEffect(() => {
    if (!cwd || !socket) return;
    queryAutoApproval(socket, cwd);
  }, [cwd, socket]);

  if (!cwd || !socket) return null;

  const mode = state?.mode ?? "user";
  const onChange = (next: string) => {
    if (next === "user" || next === "llm" || next === "full_trust") {
      setAutoApprovalMode(socket, cwd, next);
    }
  };

  return (
    <label
      className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:bg-muted/60"
      title={`智能审批 · 当前 project: ${cwd}`}
      data-testid="approval-mode-selector"
    >
      <span>审批模式</span>
      <select
        aria-label="审批模式"
        className="h-6 rounded border border-input bg-background px-1 text-xs"
        value={mode}
        onChange={(event) => onChange(event.target.value)}
        data-testid="approval-mode-select"
      >
        <option value="user">用户审批</option>
        <option value="llm">LLM 复核</option>
        <option value="full_trust">完全信任</option>
      </select>
    </label>
  );
}
