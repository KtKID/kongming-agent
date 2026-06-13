import { AlertTriangle, ShieldAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CountdownBar } from "@/features/auto-approval";
import { useApprovalInboxStore } from "./useApprovalInbox";
import type { ApprovalInboxItem } from "./types";

/**
 * 支持「本 session 都同意」按钮的 channel 白名单（v0.1 二态映射）。
 *
 * - 白名单内的 channel 渲染三按钮（拒绝 / 单次同意 / 本 session）
 * - 白名单外的 channel 仅渲染两按钮（拒绝 / 单次同意），「本 session」按钮隐藏
 *
 * 设计选择白名单而非黑名单：阶段 3-4 接入新通道（cron / evolution / cli）时
 * 默认隐藏「本 session」（保守安全），需要时显式加入；避免新通道意外暴露
 * remember-session 能力。
 *
 * 三态协议（block / elevated / silent_allow / standard）的完整支持留到
 * spec 40-protocol-evolution 阶段 5 演进。
 */
const CHANNELS_WITH_SESSION_GRANT = new Set<string>([
  "claude_code",
  // generic_chat 已接入（fix-report-20260520-generic-chat-session-grant）：
  // resolve 帧带 ``rememberScope: "session"`` + ``rememberEntry: toolName``；
  // 后端 ``ApprovalManager.resolve`` 检测 rememberScope==session → 调
  // ``ApprovalRules.add_session_grant`` 写 ``_thread_overrides``，
  // 下次同 (cwd, tool_name) 的 ``classify`` 立即 return immediate allow。
  "generic_chat",
  // 'cron' / 'evolution' / 'cli' 阶段 3-4 接入时按需加
]);

/**
 * 单条 inbox 审批卡片。
 *
 * 视觉规则：
 * - 危险卡（``blockedByRule`` 非空）：红色边框 + 顶部红色 Badge 显示规则 id + AlertTriangle 图标
 * - elevated 卡（``isElevated``）：紫色边框 + 顶部紫色 Badge "elevated" + ShieldAlert 图标
 * - 普通卡：默认边框
 *
 * 倒计时（优先级 approve > reject > fallback）：
 * - ``autoApproveAtMs`` 非空 → ``<CountdownBar mode="approve">``（绿色 → 到点自动通过）
 * - ``autoRejectAtMs`` 非空 → ``<CountdownBar mode="reject">``（红色 → 到点自动拒绝）
 * - 两者都空但 ``timeoutMs`` 非空（v0.5 fallback）→
 *     用 ``arrivedAtMs + timeoutMs`` 推 reject 倒计时（与后端 60s timeout fail-closed
 *     行为对齐，**严禁显示 approve 误导用户以为会通过**）
 * - 三者都缺（老后端 / elevated）→ 不渲染倒计时
 *
 * 三按钮：拒绝 / 单次同意 / 本 session 都同意 → 调 store.resolve（带 threadId 路由）
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

  const isBlocked = item.blockedByRule !== null;
  const isElevated = item.isElevated;
  const showSessionButton = CHANNELS_WITH_SESSION_GRANT.has(item.channel);

  // 倒计时模式选择（优先级：approve > reject > fallback；纯派生不动 store，
  // 避免 React 19 + Zustand v5 useSyncExternalStore tearing 检测引爆 #185）
  //
  // 决策说明（与 README 倒计时优先级表一致）：
  // 1. autoApproveAtMs 非空：明确的安全路径自动通过倒计时（绿）—— 最优先
  // 2. autoRejectAtMs 非空：危险路径 fail-closed 自动拒绝倒计时（橙红）
  // 3. fallback：两者都空但 timeoutMs + arrivedAtMs 都有效 → 推 reject 倒计时
  //    （后端默认 60s timeout 即 fail-closed，这里**只能 reject**不能 approve）
  // 4. 三者全缺：elevated 卡 / 老后端帧 / 兜底 → 不显示倒计时
  const hasApprove =
    typeof item.autoApproveAtMs === "number" && item.autoApproveAtMs > 0;
  const hasReject =
    typeof item.autoRejectAtMs === "number" && item.autoRejectAtMs > 0;
  const hasFallbackTimeout =
    typeof item.timeoutMs === "number" &&
    item.timeoutMs > 0 &&
    typeof item.arrivedAtMs === "number" &&
    item.arrivedAtMs > 0;

  let countdownMode: "approve" | "reject" = "reject";
  let deadline: number | null = null;
  if (hasApprove) {
    countdownMode = "approve";
    deadline = item.autoApproveAtMs;
  } else if (hasReject) {
    countdownMode = "reject";
    deadline = item.autoRejectAtMs;
  } else if (hasFallbackTimeout) {
    // fallback：arrivedAtMs + timeoutMs；reject 色调与后端 60s timeout fail-closed 对齐
    countdownMode = "reject";
    deadline = (item.arrivedAtMs as number) + (item.timeoutMs as number);
  }
  const showCountdown = deadline !== null;

  // 倒计时到点：危险 → 拒绝（fail-closed）；安全 → 同意
  const onCountdownComplete = () => {
    if (countdownMode === "reject") {
      resolve(item.threadId, item.requestId, false, { message: "auto-reject" });
    } else {
      resolve(item.threadId, item.requestId, true);
    }
  };

  // 边框/视觉色：危险 > elevated > 默认
  const cardBorderClass = isBlocked
    ? "border-destructive/40"
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

      {/* 标题行：工具名 + thread 末 8 字符 */}
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

      {/* 三按钮 */}
      <div className="flex items-center gap-1.5">
        <Button
          size="sm"
          variant="destructive"
          className="flex-1"
          onClick={() =>
            resolve(item.threadId, item.requestId, false, {
              message: "user denied",
            })
          }
          data-testid="approval-inbox-btn-reject"
        >
          拒绝
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="flex-1"
          onClick={() => resolve(item.threadId, item.requestId, true)}
          data-testid="approval-inbox-btn-allow-once"
        >
          单次同意
        </Button>
        {showSessionButton && (
          <Button
            size="sm"
            className="flex-1"
            onClick={() =>
              resolve(item.threadId, item.requestId, true, {
                rememberEntry: item.toolName,
                rememberScope: "session",
              })
            }
            data-testid="approval-inbox-btn-allow-session"
          >
            本 session
          </Button>
        )}
      </div>
    </div>
  );
}
