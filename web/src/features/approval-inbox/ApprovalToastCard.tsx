import { AlertTriangle, ShieldAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CountdownBar } from "@/features/auto-approval";
import { useApprovalInboxStore } from "./useApprovalInbox";
import type { ApprovalInboxItem } from "@/protocol";

/**
 * 单条 inbox 审批卡片。
 *
 * 视觉规则：
 * - danger 卡：强红边框与背景 + 顶部红色 Badge + AlertTriangle 图标
 * - elevated 卡（``isElevated``）：紫色边框 + 顶部紫色 Badge "elevated" + ShieldAlert 图标
 * - 普通卡：默认边框
 *
 * 人工审批 timeout 使用 reject 倒计时，到点 fail-closed。
 *
 * 普通卡提供允许一次、允许并记住、拒绝一次、拒绝并记住四个动作。
 * danger 卡隐藏记忆动作，并拦截 Enter 快捷确认，只接受显式点击。
 *
 * **不响应划走手势**（无 motion drag）、**无 × 关闭按钮**——卡片只能通过按钮决议
 * 或后端 remove 帧（用户在其他 tab 决议 / 超时 / 取消）消失。
 */

interface Props {
  item: ApprovalInboxItem;
}

function previewToolInput(input: unknown): string {
  if (input === undefined || input === null) return "";
  try {
    const s = JSON.stringify(input);
    return s.length > 200 ? s.slice(0, 200) + "…" : s;
  } catch {
    const s = String(input);
    return s.length > 200 ? s.slice(0, 200) + "…" : s;
  }
}

function shortThreadId(tid: string): string {
  /**
   * thread.id 短显示：去 `thread-` 前缀后取前 8 位 hex。
   *
   * - thread metadata 格式：`thread-<12 位 hex>`（如 `thread-797842f187aa`）
   * - 返回：`797842f1`（hex 前 8 位；与 sitian AlertCard sessionId 前 8 风格一致）
   * - 输入不含 `thread-` 前缀时（防御）：直接取前 8 字符
   * - 长度不足 8 时：原样返回
   *
   * 设计统一性参考：与 `web/src/modules/sitian/components/AlertCard.tsx:shortSessionId`
   * 同采用 slice(0, 8) 视觉风格，避免"末 8" vs "前 8" 不一致让用户认错 id 类型。
   */
  const stripped = tid.startsWith("thread-") ? tid.slice("thread-".length) : tid;
  return stripped.length > 8 ? stripped.slice(0, 8) : stripped;
}

export function ApprovalToastCard({ item }: Props) {
  const resolve = useApprovalInboxStore((s) => s.resolve);
  const submission = useApprovalInboxStore(
    (s) => s.submissionByRequestId[item.requestId],
  );
  const isSubmitting = submission?.status === "submitting";

  const isBlocked = item.danger;
  const isElevated = item.isElevated;
  const showRemember =
    !item.danger && item.rememberAllowed && item.rememberRule !== null;

  // 仅展示人工审批超时的 fail-closed 倒计时；纯派生值不写 store。
  const hasFallbackTimeout =
    typeof item.timeoutMs === "number" &&
    item.timeoutMs > 0 &&
    typeof item.arrivedAtMs === "number" &&
    item.arrivedAtMs > 0;

  const countdownMode = "reject" as const;
  const deadline = hasFallbackTimeout
    ? (item.arrivedAtMs as number) + (item.timeoutMs as number)
    : null;
  const showCountdown = deadline !== null;

  // 倒计时到点统一拒绝（fail-closed）。
  const onCountdownComplete = () => {
    resolve(item.threadId, item.requestId, false, {
      message: "approval timeout",
      remember: false,
    });
  };

  const submitRemember = (allow: boolean): void => {
    if (!showRemember || isSubmitting) return;
    void resolve(item.threadId, item.requestId, allow, {
      remember: true,
      rememberRule: item.rememberRule,
    });
  };

  // 边框/视觉色：危险 > elevated > 默认
  const cardBorderClass = isBlocked
    ? "border-destructive bg-destructive/10 ring-1 ring-destructive/40"
    : isElevated
      ? "border-purple-500/40"
      : "border-border";

  return (
    <div
      className={`w-80 rounded-xl border ${cardBorderClass} bg-card text-card-foreground shadow-lg p-3 pointer-events-auto`}
      data-testid="approval-inbox-card"
      data-request-id={item.requestId}
      data-thread-id={item.threadId}
      data-blocked={isBlocked ? "1" : "0"}
      data-elevated={isElevated ? "1" : "0"}
      data-danger={item.danger ? "1" : "0"}
      onKeyDownCapture={(event) => {
        if (item.danger && event.key === "Enter") {
          event.preventDefault();
          event.stopPropagation();
        }
      }}
    >
      {/* 顶部 Badge 行（危险 / elevated 二选一显示） */}
      {(isBlocked || isElevated) && (
        <div className="mb-2 flex items-center gap-1.5">
          {isBlocked && (
            <Badge variant="destructive" className="gap-1">
              <AlertTriangle className="h-3 w-3" />
              {item.blockedByRule}
            </Badge>
          )}
          {!isBlocked && isElevated && (
            <Badge
              variant="outline"
              className="gap-1 border-purple-500/60 text-purple-700 dark:text-purple-300"
            >
              <ShieldAlert className="h-3 w-3" />
              elevated
            </Badge>
          )}
        </div>
      )}

      {/* 标题行：工具名 + thread 前 8 字符 */}
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <div
          className="truncate text-sm font-semibold"
          data-testid="approval-inbox-tool-name"
        >
          {item.toolName}
        </div>
        <div
          className="shrink-0 font-mono text-[10px] text-muted-foreground"
          data-testid="approval-inbox-thread-short"
          title={item.threadId}
        >
          {shortThreadId(item.threadId)}
        </div>
      </div>

      {/* cwd 副标题（小字，截断） */}
      {item.cwd && (
        <div
          className="mb-2 truncate text-[10px] text-muted-foreground"
          title={item.cwd}
        >
          {item.cwd}
        </div>
      )}

      {/* 内容预览：toolInput JSON 截 200 字 */}
      <pre
        className="mb-2 max-h-24 overflow-hidden whitespace-pre-wrap break-all rounded bg-muted/60 p-2 text-[11px] leading-snug"
        data-testid="approval-inbox-tool-input"
      >
        {previewToolInput(item.toolInput)}
      </pre>

      {showRemember && item.rememberRule !== null && (
        <div className="mb-2 rounded bg-muted/40 px-2 py-1.5 text-[10px]">
          <div data-testid="approval-inbox-remember-display">
            {item.rememberRule.displayText}
          </div>
          <code
            className="block break-all text-muted-foreground"
            data-testid="approval-inbox-remember-expression"
          >
            {item.rememberRule.expression}
          </code>
          {item.rememberRule.scopeCwd !== null && (
            <code
              className="block break-all text-muted-foreground"
              data-testid="approval-inbox-remember-cwd"
            >
              目录：{item.rememberRule.scopeCwd}
            </code>
          )}
        </div>
      )}
      {!item.danger && (!item.rememberAllowed || item.rememberRule === null) && (
        <div
          className="mb-2 text-[10px] text-muted-foreground"
          data-testid="approval-inbox-remember-unavailable"
        >
          当前请求只支持单次审批；服务端未生成安全记忆范围
        </div>
      )}
      {isSubmitting && (
        <div
          className="mb-2 text-[10px]"
          data-testid="approval-inbox-remember-loading"
        >
          正在提交…
        </div>
      )}
      {submission?.status === "error" && (
        <div
          className="mb-2 text-[10px] text-destructive"
          data-testid="approval-inbox-remember-error"
        >
          {submission.message ?? "审批提交失败，请重试"}
        </div>
      )}

      {/* 倒计时（elevated 无倒计时） */}
      {showCountdown && deadline !== null && (
        <div className="mb-2" data-testid="approval-inbox-countdown">
          <CountdownBar
            deadlineMs={deadline}
            mode={countdownMode}
            onComplete={onCountdownComplete}
            showSeconds
            heightPx={3}
          />
        </div>
      )}

      {/* 普通卡四动作；danger 卡只保留显式的一次允许/拒绝。 */}
      <div className="grid grid-cols-2 gap-1.5">
        <Button
          size="sm"
          variant="destructive"
          className="flex-1"
          disabled={isSubmitting}
          onClick={() =>
            resolve(item.threadId, item.requestId, false, {
              message: "user denied",
              remember: false,
            })
          }
          data-testid="approval-inbox-btn-reject"
        >
          拒绝一次
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="flex-1"
          disabled={isSubmitting}
          onClick={() =>
            resolve(item.threadId, item.requestId, true, { remember: false })
          }
          data-testid="approval-inbox-btn-allow-once"
        >
          允许一次
        </Button>
        {showRemember && (
          <Button
            size="sm"
            className="flex-1"
            disabled={isSubmitting}
            onClick={() => submitRemember(true)}
            data-testid="approval-inbox-btn-allow-remember"
          >
            允许并记住
          </Button>
        )}
        {showRemember && (
          <Button
            size="sm"
            variant="destructive"
            className="flex-1"
            disabled={isSubmitting}
            onClick={() => submitRemember(false)}
            data-testid="approval-inbox-btn-deny-remember"
          >
            拒绝并记住
          </Button>
        )}
      </div>
    </div>
  );
}
