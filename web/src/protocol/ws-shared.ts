export type ReasoningEffort = "none" | "low" | "medium" | "high" | "max";

export type CodexPermissionMode =
  | "default"
  | "acceptEdits"
  | "bypassPermissions";

export interface AbortSessionFrame {
  frame_type: "abort-session";
  sessionId: string;
  provider?: string | null;
}

export interface CheckSessionStatusFrame {
  frame_type: "check-session-status";
  sessionId: string;
  provider?: string | null;
}

export interface SessionStatusFrame {
  frame_type: "session-status";
  sessionId: string;
  isProcessing: boolean;
}

export interface AutoApprovalSetModeFrame {
  frame_type: "auto-approval-set-mode";
  cwd: string;
  mode: "user" | "llm" | "full_trust";
}

export interface AutoApprovalQueryFrame {
  frame_type: "auto-approval-query";
  cwd: string;
}

export interface AutoApprovalStateFrame {
  frame_type: "auto_approval_state";
  channel: "claude_code" | "generic_chat";
  cwd: string;
  mode: "user" | "llm" | "full_trust";
  timeoutMs: number;
  ruleOverrides: Record<string, boolean>;
}

export type AutoApprovalStateWireFrame = AutoApprovalStateFrame;
