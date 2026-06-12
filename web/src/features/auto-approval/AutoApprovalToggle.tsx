import { useEffect } from "react";
import { Zap, ZapOff } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import {
  type AutoApprovalSocket,
  queryAutoApproval,
  selectCwdState,
  toggleAutoApproval,
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

export function AutoApprovalToggle({ cwd, socket }: Props) {
  const state = useAutoApprovalStore(selectCwdState(cwd));

  // 挂载 / cwd 变更 / socket 变更 → 主动 query 一次
  useEffect(() => {
    if (!cwd || !socket) return;
    queryAutoApproval(socket, cwd);
  }, [cwd, socket]);

  if (!cwd || !socket) return null;

  const enabled = state?.enabled ?? false;
  const seconds = Math.round((state?.timeoutMs ?? 10000) / 1000);

  const onChange = (next: boolean) => {
    toggleAutoApproval(socket, cwd, next);
  };

  return (
    <label
      className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:bg-muted/60"
      title={`智能审批 · 当前 project: ${cwd}`}
      data-testid="auto-approval-toggle"
    >
      {enabled ? (
        <Zap className="h-3.5 w-3.5 text-primary" />
      ) : (
        <ZapOff className="h-3.5 w-3.5 opacity-50" />
      )}
      <span className={enabled ? "text-primary" : ""}>
        智能审批
        {enabled ? (
          <span className="ml-1 text-[10px] opacity-80">{seconds}s</span>
        ) : null}
      </span>
      <Switch
        checked={enabled}
        onCheckedChange={onChange}
        aria-label="切换智能审批"
        data-testid="auto-approval-switch"
      />
    </label>
  );
}
