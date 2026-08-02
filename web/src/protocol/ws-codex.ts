import type { NormalizedMessage, UserInputAttachment } from "../protocol";
import type {
  AbortSessionFrame,
  CheckSessionStatusFrame,
  CodexPermissionMode,
  ReasoningEffort,
  SessionStatusFrame,
} from "./ws-shared";

export interface CodexCommandOptions {
  cwd?: string | null;
  sessionId?: string | null;
  resume?: boolean | null;
  model?: string | null;
  permissionMode?: CodexPermissionMode | null;
  reasoningEffort?: ReasoningEffort | null;
}

export interface CodexCommandFrame {
  frame_type: "codex-command";
  command: string;
  options?: CodexCommandOptions | null;
  attachments?: UserInputAttachment[] | null;
}

export type CodexC2SFrame =
  | CodexCommandFrame
  | AbortSessionFrame
  | CheckSessionStatusFrame;

export type CodexS2CFrame = NormalizedMessage | SessionStatusFrame;
