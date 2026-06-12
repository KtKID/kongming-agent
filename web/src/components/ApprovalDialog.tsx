import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AlertTriangle } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useApprovalDialogStore } from "@/hooks/useApprovalDialog";
import type { ApprovalAckAction } from "@/protocol";

export interface ApprovalAckSocket {
  send(frame: {
    frame_type: "approval.ack";
    call_id: string;
    action: ApprovalAckAction;
  }): boolean | void;
}

/**
 * 审批 modal（v0.1.6 三按钮 + elevated 模式）：
 *
 * - 监听 useApprovalDialogStore.pending 队列；非空时弹起队头
 * - 显示 tool_name / arguments(JSON pretty) / reason
 * - **standard 模式**（默认）：三按钮（同意 / 本 session 同意 / 拒绝）
 * - **elevated 模式**（policy_hint === "elevated"）：
 *   - 隐藏「本 session 同意」按钮（防止 grant 扩散）
 *   - 「同意」需用户输入 confirm_token（8 hex）后才可点击
 *   - 红色边框 + 警告图标
 * - **ESC 键 = 拒绝**（安全约定：默认拒绝，避免误确认）
 * - blocking modal（点遮罩不关；只能用按钮 / ESC）
 */
export function ApprovalDialog({ socket }: { socket: ApprovalAckSocket | null }) {
  const head = useApprovalDialogStore((s) => s.pending[0]);
  const shift = useApprovalDialogStore((s) => s.shift);
  const [busy, setBusy] = useState(false);
  const [tokenInput, setTokenInput] = useState("");

  const isElevated = head?.policy_hint === "elevated";
  const expectedToken = head?.confirm_token ?? "";
  const tokenMatched = !isElevated || tokenInput === expectedToken;

  // 每次 head 切换时清空 token 输入
  useEffect(() => {
    setTokenInput("");
  }, [head?.call_id]);

  // ESC 拦截 = 拒绝（不是 close）
  useEffect(() => {
    if (!head) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        respond("reject");
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [head, socket]);

  const respond = (action: ApprovalAckAction) => {
    if (busy || !head) return;
    setBusy(true);
    try {
      if (!socket) {
        toast.error("审批连接未就绪，请稍后重试");
        return;
      }
      const sent = socket.send({
        frame_type: "approval.ack",
        call_id: head.call_id,
        action,
      });
      if (sent === false) {
        toast.error("审批发送失败，请稍后重试");
        return;
      }
      shift();
    } catch (err) {
      console.error("[ApprovalDialog] approval ack send failed", err);
      toast.error("审批发送失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  if (!head) return null;

  return (
    <Dialog
      open={true}
      onOpenChange={(open) => {
        // blocking：试图关闭（ESC / 点遮罩）一律视为 reject
        if (!open) respond("reject");
      }}
    >
      <DialogContent
        onPointerDownOutside={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
        className={isElevated ? "border-2 border-destructive" : undefined}
      >
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18 }}
        >
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {isElevated && (
                <AlertTriangle
                  className="h-5 w-5 text-destructive"
                  data-testid="elevated-warning-icon"
                />
              )}
              {isElevated ? "危险操作审批" : "需要审批"}：{head.tool_name}
            </DialogTitle>
            <DialogDescription>
              {head.reason ?? "工具调用需要你的允许才能执行"}
            </DialogDescription>
          </DialogHeader>
          <div className="my-3">
            <pre
              className="max-h-60 overflow-y-auto overflow-x-hidden whitespace-pre-wrap break-all rounded-md border border-border bg-muted p-3 font-mono text-xs"
              data-testid="approval-arguments"
            >
              {JSON.stringify(head.arguments, null, 2)}
            </pre>
          </div>
          {isElevated && expectedToken && (
            <div className="my-3" data-testid="confirm-token-area">
              <p className="mb-2 text-sm text-destructive font-medium">
                输入确认码 <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{expectedToken}</code> 以确认操作
              </p>
              <Input
                placeholder="输入确认码..."
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                className="font-mono"
                data-testid="confirm-token-input"
                autoFocus
              />
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => respond("reject")}
              disabled={busy}
              data-testid="approval-reject"
            >
              拒绝
            </Button>
            {!isElevated && (
              <Button
                variant="outline"
                onClick={() => respond("accept_for_session")}
                disabled={busy}
                data-testid="approval-approve-session"
              >
                本 session 同意
              </Button>
            )}
            <Button
              onClick={() => respond("accept_once")}
              disabled={busy || !tokenMatched}
              variant={isElevated ? "destructive" : "default"}
              data-testid="approval-approve"
            >
              同意
            </Button>
          </DialogFooter>
        </motion.div>
      </DialogContent>
    </Dialog>
  );
}
