import { useEffect, useRef, useState } from "react";

/**
 * 倒计时进度条（smart-approval-v1 + v1.0.1）。
 *
 * 输入：``deadlineMs``（绝对时间戳 ms，来自后端 ``autoApproveAtMs`` 或 ``autoRejectAtMs``）。
 * 行为：
 *
 * - 每 100ms 用 ``Date.now()`` 自算剩余 → 不依赖 server-client 时钟同步
 * - 切 tab 回来仍准（``Date.now()`` 即时取值）
 * - 到点（remaining <= 0）调一次 ``onComplete``，**只调一次**（用 ref 守卫）
 * - WS 断线重连 + 后端重发 ``deadlineMs``（若已过期，挂载即触发 onComplete）
 *
 * 设计：仅做时间显示与 hook 触发，不做"按下哪个按钮"的决定——上层 dialog
 * 负责接 ``onComplete``：
 * - ``mode="approve"`` 调"同意"逻辑（v1 安全路径自动通过）
 * - ``mode="reject"`` 调"拒绝"逻辑（v1.0.1 危险路径自动拒绝，fail-closed）
 */
export type CountdownMode = "approve" | "reject";

interface Props {
  /** 绝对截止时间 ms（来自后端 ``autoApproveAtMs`` 或 ``autoRejectAtMs``） */
  deadlineMs: number;
  /** 倒计时归零时回调；上层应当 idempotent 处理 */
  onComplete: () => void;
  /** v1.0.1: 区分通过/拒绝两种倒计时（决定进度条颜色 + 文案） */
  mode?: CountdownMode;
  /** 进度条高度 px，默认 4 */
  heightPx?: number;
  /** 显示秒数（默认 true）；false 时仅显示进度条 */
  showSeconds?: boolean;
  /** 给单测注入"假 now"用；生产请不传，默认走 Date.now() */
  nowProvider?: () => number;
}

export function CountdownBar({
  deadlineMs,
  onComplete,
  mode = "approve",
  heightPx = 4,
  showSeconds = true,
  nowProvider,
}: Props) {
  const now = nowProvider ?? (() => Date.now());
  const [remaining, setRemaining] = useState(() =>
    Math.max(0, deadlineMs - now()),
  );
  // 初始 totalMs 锁定（避免 props 抖动改变除数）
  const totalMsRef = useRef<number>(
    Math.max(1, deadlineMs - now()), // 防 0 除
  );
  // 防重复触发 onComplete
  const completedRef = useRef(false);

  useEffect(() => {
    // 切 props deadline 时重置总时长 + 完成标志
    completedRef.current = false;
    totalMsRef.current = Math.max(1, deadlineMs - now());
    setRemaining(Math.max(0, deadlineMs - now()));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deadlineMs]);

  useEffect(() => {
    const tick = () => {
      const r = Math.max(0, deadlineMs - now());
      setRemaining(r);
      if (r <= 0 && !completedRef.current) {
        completedRef.current = true;
        onComplete();
      }
    };
    // 挂载立即 tick 一次（重连场景：deadline 可能已过期）
    tick();
    const id = window.setInterval(tick, 100);
    return () => {
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deadlineMs, onComplete]);

  // 进度比例：剩余 / 总时长
  const pct = Math.max(
    0,
    Math.min(100, Math.round((remaining / totalMsRef.current) * 100)),
  );
  const secondsLeft = Math.ceil(remaining / 1000);

  // v1.0.1: reject 模式 → 红色 destructive；approve 模式 → 绿色 primary
  const isReject = mode === "reject";
  const barColorClass = isReject ? "bg-destructive" : "bg-primary";
  const labelText = isReject ? `${secondsLeft}s 自动拒绝` : `${secondsLeft}s 自动通过`;
  const labelColorClass = isReject ? "text-destructive" : "text-muted-foreground";

  return (
    <div className="w-full" data-testid="countdown-bar" data-mode={mode}>
      {showSeconds && (
        <div className={`mb-1 flex items-center justify-between text-[11px] ${labelColorClass}`}>
          <span>⚡ 智能审批</span>
          <span data-testid="countdown-seconds">{labelText}</span>
        </div>
      )}
      <div
        className="w-full overflow-hidden rounded-full bg-muted"
        style={{ height: heightPx }}
      >
        <div
          className={`h-full rounded-full ${barColorClass} transition-[width] duration-100 ease-linear`}
          style={{ width: `${pct}%` }}
          data-testid="countdown-progress"
        />
      </div>
    </div>
  );
}
