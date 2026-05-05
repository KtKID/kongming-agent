import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/**
 * Claude Code 路径的工具审批 dialog（v0.1.6）。
 *
 * 输入：normalize 出来的 `permission_request` 帧字段（requestId / toolName /
 * toolInput）。三种结局：
 *
 * - 拒绝 → `{ allow: false, message: "user denied" }`
 * - 单次允许 → `{ allow: true }`
 * - 本 session 都允许 → `{ allow: true, rememberEntry: toolName }`
 *
 * 帧字段对齐 `web.claude_code.approval` 内 ApprovalBridge.resolve 接受的形态。
 */
export interface ClaudeApprovalRequest {
  requestId: string;
  toolName: string;
  toolInput?: unknown;
}

export interface ClaudeApprovalResponse {
  requestId: string;
  allow: boolean;
  message?: string;
  rememberEntry?: string;
}

interface Props {
  open: boolean;
  request: ClaudeApprovalRequest | null;
  onResolve: (response: ClaudeApprovalResponse) => void;
}

export function ClaudeApprovalDialog({ open, request, onResolve }: Props) {
  const handleReject = () => {
    if (!request) return;
    onResolve({
      requestId: request.requestId,
      allow: false,
      message: "user denied",
    });
  };

  const handleAllowOnce = () => {
    if (!request) return;
    onResolve({ requestId: request.requestId, allow: true });
  };

  const handleAllowSession = () => {
    if (!request) return;
    onResolve({
      requestId: request.requestId,
      allow: true,
      rememberEntry: request.toolName,
    });
  };

  return (
    <Dialog
      open={open && !!request}
      onOpenChange={(v) => {
        // 关闭 dialog（点遮罩 / ESC）等同拒绝
        if (!v) handleReject();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Claude 申请使用工具</DialogTitle>
          <DialogDescription>
            {request ? `工具：${request.toolName}` : ""}
          </DialogDescription>
        </DialogHeader>
        {request?.toolInput !== undefined && request?.toolInput !== null ? (
          <pre className="max-h-72 overflow-auto rounded bg-muted p-3 text-xs">
            {(() => {
              try {
                return JSON.stringify(request.toolInput, null, 2);
              } catch {
                return String(request.toolInput);
              }
            })()}
          </pre>
        ) : null}
        <DialogFooter className="gap-2 sm:gap-2">
          <Button variant="destructive" onClick={handleReject}>
            拒绝
          </Button>
          <Button variant="outline" onClick={handleAllowOnce}>
            单次允许
          </Button>
          <Button onClick={handleAllowSession}>本 session 都允许</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
