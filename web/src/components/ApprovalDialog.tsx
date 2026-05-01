import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useApprovalDialogStore } from "@/hooks/useApprovalDialog";
import type { ThreadSocket } from "@/lib/ws";
import type { ApprovalAckAction } from "@/protocol";

/**
 * 审批 modal（v0.1.6 三按钮）：
 *
 * - 监听 useApprovalDialogStore.pending 队列；非空时弹起队头
 * - 显示 tool_name / arguments(JSON pretty) / reason
 * - 三按钮 → 推 `approval.ack` 给 ws → shift 队列：
 *   - 同意（accept_once）：仅本次放行
 *   - 本 session 同意（accept_for_session）：本 thread 内同 capability 后续静默放行
 *   - 拒绝（reject）：拦下；ESC / 点遮罩同 reject
 * - **ESC 键 = 拒绝**（安全约定：默认拒绝，避免误确认）
 * - blocking modal（点遮罩不关；只能用按钮 / ESC）
 */
export function ApprovalDialog({ socket }: { socket: ThreadSocket | null }) {
  const head = useApprovalDialogStore((s) => s.pending[0]);
  const shift = useApprovalDialogStore((s) => s.shift);
  const [busy, setBusy] = useState(false);

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
      if (socket) {
        socket.send({
          kind: "approval.ack",
          call_id: head.call_id,
          action,
        });
      }
    } finally {
      shift();
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
      >
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18 }}
        >
          <DialogHeader>
            <DialogTitle>需要审批：{head.tool_name}</DialogTitle>
            <DialogDescription>
              {head.reason ?? "工具调用需要你的允许才能执行"}
            </DialogDescription>
          </DialogHeader>
          <div className="my-3">
            <pre className="max-h-60 overflow-auto rounded-md border border-border bg-muted p-3 font-mono text-xs">
              {JSON.stringify(head.arguments, null, 2)}
            </pre>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => respond("reject")}
              disabled={busy}
              data-testid="approval-reject"
            >
              拒绝
            </Button>
            <Button
              variant="outline"
              onClick={() => respond("accept_for_session")}
              disabled={busy}
              data-testid="approval-approve-session"
            >
              本 session 同意
            </Button>
            <Button
              onClick={() => respond("accept_once")}
              disabled={busy}
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
