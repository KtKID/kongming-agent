/**
 * smart-approval-v1 协议类型（前端 ↔ 后端对齐）。
 *
 * 后端真源：
 * - claude_code dict 协议：src/web/claude_code/route.py::_build_auto_approval_state_msg
 * - permission_request 新增字段：src/web/claude_code/approval.py
 */

/** 后端 → 前端：smart-approval 当前 cwd 的配置状态 */
export interface AutoApprovalStateFrame {
  frame_type: "auto_approval_state";
  channel: "claude_code" | "generic_chat"; // v1 仅 claude_code
  cwd: string;
  enabled: boolean;
  timeoutMs: number;
  ruleOverrides: Record<string, boolean>;
}

/** 前端 → 后端：开关 toggle */
export interface AutoApprovalToggleFrame {
  frame_type: "auto-approval-toggle";
  cwd: string;
  enabled: boolean;
}

/** 前端 → 后端：主动查询 cwd 的状态（页面挂载 / 切换 thread 用） */
export interface AutoApprovalQueryFrame {
  frame_type: "auto-approval-query";
  cwd: string;
}

/** permission_request 帧的 v1 + v1.0.1 新增字段（已 patch 进 NormalizedMessage） */
export interface PermissionRequestSmartFields {
  /** v1：绝对时间戳（毫秒）；非空 = 安全路径倒计时自动**通过**；空 = 必须人审 */
  autoApproveAtMs?: number | null;
  /**
   * v1.0.1：绝对时间戳（毫秒）；非空 = 危险路径倒计时自动**拒绝**（fail-closed）；空 = 必须人审。
   *
   * 与 `autoApproveAtMs` 互斥：要么都为 null（人审），要么有且仅有一个非 null。
   */
  autoRejectAtMs?: number | null;
  /** 命中的危险规则 id（用于 banner）；空 = 未命中 */
  blockedByRule?: string | null;
  /** 通道标识（v1 只有 claude_code） */
  channel?: string;
}
