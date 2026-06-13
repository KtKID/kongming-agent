/**
 * dashboard/config 子模块内部组件，**不要被 sibling 模块直接 import**。
 *
 * ConfigPage 底部固定操作栏：
 * - 左侧：脏字段计数 + 校验错误（前 3 条 + "+N more"）+ 保存状态文字 + 重启错误提示
 * - 右侧：保存按钮 + 重启按钮
 * - sticky 在 ConfigPage 容器底部（不是 viewport / page bottom）
 *
 * ## 状态来源
 *
 * 全部来自 `../store.ts`（zustand）。本组件**不直接调 fetch**，只触发
 * store actions（`save()` / `restart()`）。订阅采用单字段 selector
 * （`useConfigStore(s => s.dirtyCount)`），避免整 store 订阅触发非相关 rerender。
 *
 * ## reload 副作用
 *
 * `restartStatus === "ready"` 是健康轮询成功的终态，store 不动 DOM。
 * 本组件用一个 `useEffect` 监听该状态，延迟 500ms 调
 * `window.location.reload()`，让用户先看到 "服务已恢复" 字样。
 * `restartStatus === "timeout"` 时给一个 "手动刷新" 兜底按钮，点击直接 reload。
 *
 * ## 设计取舍
 *
 * - 保存按钮文案 / disabled 完全由 (`dirtyCount`, `saveStatus`) 派生，不在
 *   本组件保留独立 local state，避免和 store 出现不一致；
 * - `saveStatus === "saved"` 的回切 idle 由 store 负责（或下次 setField 触发
 *   `dirtyCount` 变化）；本组件只显示 "已保存"，不在 UI 层 setTimeout 回切，
 *   遵守 "store 之外不做副作用" 的边界；
 * - 不引入 emoji（用户全局禁止），状态用颜色 token + 文字表达。
 */

import { useEffect, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { useConfigStore } from "../store";

/** ready → reload 之间留给用户看到 "服务已恢复" 的延迟（ms）。 */
const READY_RELOAD_DELAY_MS = 500;
/** validationErrors 列表最多渲染条数，剩余折叠成 "+N more"。 */
const VALIDATION_ERROR_DISPLAY_LIMIT = 3;

export interface SaveRestartBarProps {
  /** 额外 class，方便宿主页面调整边距 / 阴影 */
  className?: string;
}

export function SaveRestartBar({ className }: SaveRestartBarProps): ReactNode {
  // —— 单字段订阅，避免整 store rerender —————————————————
  const dirtyCount = useConfigStore((s) => s.dirtyCount);
  const saveStatus = useConfigStore((s) => s.saveStatus);
  const validationErrors = useConfigStore((s) => s.validationErrors);
  const saveError = useConfigStore((s) => s.saveError);
  const pendingRestartFields = useConfigStore((s) => s.pendingRestartFields);
  const restartStatus = useConfigStore((s) => s.restartStatus);
  const restartError = useConfigStore((s) => s.restartError);
  const save = useConfigStore((s) => s.save);
  const restart = useConfigStore((s) => s.restart);

  // —— restartStatus=ready → 延迟 reload —————————————————
  useEffect(() => {
    if (restartStatus !== "ready") return;
    const handle = window.setTimeout(() => {
      window.location.reload();
    }, READY_RELOAD_DELAY_MS);
    return () => {
      window.clearTimeout(handle);
    };
  }, [restartStatus]);

  return (
    <div
      className={cn(
        "sticky bottom-0 left-0 right-0 z-20 flex flex-wrap items-center gap-3",
        "border-t border-border/70 bg-card/95 px-4 py-3 shadow-[0_-4px_16px_-12px_rgba(0,0,0,0.18)] backdrop-blur-xl",
        className,
      )}
      data-testid="save-restart-bar"
    >
      <LeftStatus
        dirtyCount={dirtyCount}
        saveStatus={saveStatus}
        validationErrors={validationErrors}
        saveError={saveError}
        restartStatus={restartStatus}
        restartError={restartError}
      />
      <div className="ml-auto flex items-center gap-2">
        <SaveButton
          dirtyCount={dirtyCount}
          saveStatus={saveStatus}
          onSave={save}
        />
        <RestartButton
          pendingRestartFields={pendingRestartFields}
          restartStatus={restartStatus}
          onRestart={restart}
        />
      </div>
    </div>
  );
}

// ─── 左侧状态文字 ──────────────────────────────────────────────────────────

interface LeftStatusProps {
  dirtyCount: number;
  saveStatus: ReturnType<typeof useConfigStore.getState>["saveStatus"];
  validationErrors: ReturnType<
    typeof useConfigStore.getState
  >["validationErrors"];
  saveError: string | null;
  restartStatus: ReturnType<typeof useConfigStore.getState>["restartStatus"];
  restartError: string | null;
}

function LeftStatus({
  dirtyCount,
  saveStatus,
  validationErrors,
  saveError,
  restartStatus,
  restartError,
}: LeftStatusProps): ReactNode {
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">
          {dirtyCount === 0
            ? "无未保存改动"
            : `${dirtyCount} 个待保存改动`}
        </span>
        <SaveStatusText status={saveStatus} saveError={saveError} />
        <RestartStatusText
          status={restartStatus}
          restartError={restartError}
        />
      </div>
      {saveStatus === "validation_error" && validationErrors.length > 0 ? (
        <ValidationErrorList errors={validationErrors} />
      ) : null}
    </div>
  );
}

function SaveStatusText({
  status,
  saveError,
}: {
  status: LeftStatusProps["saveStatus"];
  saveError: string | null;
}): ReactNode {
  switch (status) {
    case "saving":
      return <span className="text-muted-foreground">保存中...</span>;
    case "saved":
      return (
        <span className="text-emerald-600 dark:text-emerald-300">已保存</span>
      );
    case "validation_error":
      return (
        <span className="text-rose-600 dark:text-rose-300">
          字段校验失败
        </span>
      );
    case "conflict":
      return (
        <span className="text-amber-600 dark:text-amber-300">
          配置已被外部修改，请刷新
        </span>
      );
    case "error":
      return (
        <span className="text-rose-600 dark:text-rose-300">
          保存失败{saveError ? `：${saveError}` : ""}
        </span>
      );
    case "idle":
    default:
      return null;
  }
}

function RestartStatusText({
  status,
  restartError,
}: {
  status: LeftStatusProps["restartStatus"];
  restartError: string | null;
}): ReactNode {
  switch (status) {
    case "restarting":
      return (
        <span className="text-muted-foreground">正在触发重启...</span>
      );
    case "polling_health":
      return (
        <span className="text-muted-foreground">
          重启中，等待服务恢复...
        </span>
      );
    case "ready":
      return (
        <span className="text-emerald-600 dark:text-emerald-300">
          服务已恢复
        </span>
      );
    case "timeout":
      return (
        <span className="text-rose-600 dark:text-rose-300">
          重启超时{restartError ? `：${restartError}` : ""}
        </span>
      );
    case "idle":
    default:
      return null;
  }
}

function ValidationErrorList({
  errors,
}: {
  errors: LeftStatusProps["validationErrors"];
}): ReactNode {
  const head = errors.slice(0, VALIDATION_ERROR_DISPLAY_LIMIT);
  const more = errors.length - head.length;
  return (
    <ul
      className="flex flex-col gap-0.5 text-[11px] text-rose-600 dark:text-rose-300"
      data-testid="validation-error-list"
    >
      {head.map((err, idx) => (
        <li key={`${err.path}-${idx}`} className="truncate">
          <span className="font-mono">{err.path}</span>
          <span className="mx-1">·</span>
          <span>{err.message}</span>
        </li>
      ))}
      {more > 0 ? (
        <li className="text-rose-500/80 dark:text-rose-300/80">
          +{more} more
        </li>
      ) : null}
    </ul>
  );
}

// ─── 右侧按钮 ──────────────────────────────────────────────────────────────

interface SaveButtonProps {
  dirtyCount: number;
  saveStatus: LeftStatusProps["saveStatus"];
  onSave: () => void | Promise<void>;
}

function SaveButton({
  dirtyCount,
  saveStatus,
  onSave,
}: SaveButtonProps): ReactNode {
  const isSaving = saveStatus === "saving";
  const isSaved = saveStatus === "saved";
  const isConflict = saveStatus === "conflict";

  // 仅以下场景禁用：保存进行中 / saved 终态 / conflict（必须刷新）/ 没东西可保存
  const disabled =
    isSaving || isSaved || isConflict || (dirtyCount === 0 && saveStatus !== "error" && saveStatus !== "validation_error");

  let label: string;
  if (isSaving) {
    label = "保存中...";
  } else if (isSaved) {
    label = "已保存";
  } else if (saveStatus === "validation_error") {
    label = "重新保存";
  } else if (saveStatus === "error") {
    label = "重试保存";
  } else if (dirtyCount > 0) {
    label = `保存 ${dirtyCount} 项`;
  } else {
    label = "保存";
  }

  return (
    <Button
      type="button"
      size="sm"
      variant="default"
      disabled={disabled}
      onClick={() => {
        void onSave();
      }}
      data-testid="save-button"
    >
      {label}
    </Button>
  );
}

interface RestartButtonProps {
  pendingRestartFields: string[];
  restartStatus: LeftStatusProps["restartStatus"];
  onRestart: () => void | Promise<void>;
}

function RestartButton({
  pendingRestartFields,
  restartStatus,
  onRestart,
}: RestartButtonProps): ReactNode {
  const hasPending = pendingRestartFields.length > 0;
  const isRestarting = restartStatus === "restarting";
  const isPolling = restartStatus === "polling_health";
  const isReady = restartStatus === "ready";
  const isTimeout = restartStatus === "timeout";

  // timeout 时切换为 "手动刷新" 兜底按钮，点击直接 reload
  if (isTimeout) {
    return (
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={() => {
          window.location.reload();
        }}
        data-testid="manual-reload-button"
      >
        手动刷新
      </Button>
    );
  }

  let label: string;
  if (isRestarting) {
    label = "正在触发重启...";
  } else if (isPolling) {
    label = "等待服务恢复...";
  } else if (isReady) {
    label = "服务已恢复";
  } else if (hasPending) {
    label = `重启服务（${pendingRestartFields.length} 项需重启）`;
  } else {
    label = "重启";
  }

  const disabled = !hasPending || isRestarting || isPolling || isReady;

  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      disabled={disabled}
      onClick={() => {
        void onRestart();
      }}
      data-testid="restart-button"
    >
      {label}
    </Button>
  );
}
