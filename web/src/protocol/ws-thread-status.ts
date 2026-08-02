import type { PingFrame, PongFrame, UsageSummaryUpdatedFrame } from "../protocol";
import type { ThreadStatusFrame } from "./generated/thread-status-frame";
import type { ThreadStatusSnapshotFrame } from "./generated/thread-status-snapshot-frame";

export type { ThreadStatusFrame } from "./generated/thread-status-frame";
export type { ThreadStatusSnapshotFrame } from "./generated/thread-status-snapshot-frame";

export type ThreadStatusPhase = ThreadStatusFrame["phase"];

/**
 * run 结束原因 bitmask（与后端 core.result.RunEndReason 1:1 对齐，约束 17 双侧真源）。
 *
 * 自然因（三选一互斥）：COMPLETE / MAX_TURNS / ERROR。
 * 外部因（可叠加）：INTERRUPT / EVICTED。
 *
 * - 按钮复位：reason > 0 即复位，不关心具体原因。
 * - UI 显示：INTERRUPT 位 set 时显示"已停止"（尊重用户介入）。
 */
export const RUN_END_REASON = {
  NONE: 0,
  COMPLETE: 1,
  MAX_TURNS: 2,
  ERROR: 4,
  INTERRUPT: 8,
  EVICTED: 16,
} as const;

export interface RememberRule {
  expression: string;
  displayText: string;
  scopeCwd: string | null;
}

export interface ApprovalInboxItem {
  requestId: string;
  threadId: string;
  toolName: string;
  toolInput: unknown;
  blockedByRule: string | null;
  isElevated: boolean;
  danger: boolean;
  rememberAllowed: boolean;
  channel: string;
  cwd: string;
  arrivedAtMs: number;
  timeoutMs?: number | null;
  autoApproveAtMs?: number | null;
  autoRejectAtMs?: number | null;
  rememberRule: RememberRule | null;
}

export interface ApprovalInboxAddFrame extends ApprovalInboxItem {
  frame_type: "approval.inbox.add";
}

export type ApprovalInboxRemoveReason =
  | "user_decided"
  | "timeout"
  | "cancelled"
  | "auto_allowed";

export interface ApprovalInboxRemoveFrame {
  frame_type: "approval.inbox.remove";
  requestId: string;
  reason: ApprovalInboxRemoveReason;
}

export interface ApprovalInboxSnapshotFrame {
  frame_type: "approval.inbox.snapshot";
  items: ApprovalInboxItem[];
}

export interface ApprovalInboxResolveFrame {
  frame_type: "approval.inbox.resolve";
  threadId: string;
  requestId: string;
  allow: boolean;
  remember: boolean;
  rememberRule?: RememberRule | null;
  message?: string | null;
}

export interface ApprovalInboxResolveResultFrame {
  frame_type: "approval.inbox.resolve_result";
  requestId: string;
  accepted: boolean;
  message?: string | null;
}

export type ThreadStatusC2SFrame = PingFrame | ApprovalInboxResolveFrame;

export type ThreadStatusS2CFrame =
  | PongFrame
  | ThreadStatusFrame
  | ThreadStatusSnapshotFrame
  | UsageSummaryUpdatedFrame
  | ApprovalInboxAddFrame
  | ApprovalInboxRemoveFrame
  | ApprovalInboxSnapshotFrame
  | ApprovalInboxResolveResultFrame;
