import type { NormalizedMessage, UserInputAttachment } from "../protocol";
import type {
  AbortSessionFrame,
  AutoApprovalQueryFrame,
  AutoApprovalStateFrame,
  AutoApprovalSetModeFrame,
  CheckSessionStatusFrame,
  SessionStatusFrame,
} from "./ws-shared";

export type ClaudeCodeC2SFrame =
  | {
      frame_type: "claude-command";
      command: string;
      options?: Record<string, unknown>;
      attachments?: UserInputAttachment[];
    }
  | AbortSessionFrame
  | CheckSessionStatusFrame
  | AutoApprovalSetModeFrame
  | AutoApprovalQueryFrame;

export type ClaudeCodeS2CFrame =
  | NormalizedMessage
  | SessionStatusFrame
  | AutoApprovalStateFrame;
