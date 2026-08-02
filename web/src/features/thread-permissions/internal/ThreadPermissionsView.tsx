/**
 * thread permissions 内部视图。
 *
 * 仅渲染 Manager 提供的快照、草稿与状态，不直接访问 REST 或持有持久化状态。
 */

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import type { PermissionRuleDTO, ThreadPermissionsDTO } from "@/protocol";

import type { ThreadPermissionsStatus } from "../ThreadPermissionsManager";

interface ThreadPermissionsViewProps {
  threadId: string;
  snapshot: ThreadPermissionsDTO | null;
  status: ThreadPermissionsStatus;
  allowDraft: PermissionRuleDTO[];
  denyDraft: PermissionRuleDTO[];
  errorMessage: string | null;
  loadFailed: boolean;
  onAllowDraftChange: (value: PermissionRuleDTO[]) => void;
  onDenyDraftChange: (value: PermissionRuleDTO[]) => void;
  onSave: () => Promise<void>;
  onReload: () => Promise<void>;
}

export function ThreadPermissionsView({
  threadId,
  snapshot,
  status,
  allowDraft,
  denyDraft,
  errorMessage,
  loadFailed,
  onAllowDraftChange,
  onDenyDraftChange,
  onSave,
  onReload,
}: ThreadPermissionsViewProps): ReactNode {
  if (status === "loading") {
    return (
      <div data-testid="thread-permissions-loading" role="status">
        正在加载 thread 审批本子…
      </div>
    );
  }

  if (loadFailed || snapshot === null) {
    return (
      <section className="space-y-3" data-testid="thread-permissions-load-error">
        <p className="text-sm text-destructive" role="alert">
          加载审批本子失败：{errorMessage ?? "未知错误"}
        </p>
        <Button type="button" variant="outline" onClick={() => void onReload()}>
          重试
        </Button>
      </section>
    );
  }

  const isEmpty = snapshot.allow.length === 0 && snapshot.deny.length === 0;
  const isSaving = status === "saving";

  const renderRules = (
    verdict: "allow" | "deny",
    rules: PermissionRuleDTO[],
    onChange: (value: PermissionRuleDTO[]) => void,
  ): ReactNode => (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium">
          {verdict === "allow" ? "允许规则" : "拒绝规则"}
        </h4>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={isSaving}
          onClick={() => onChange([...rules, { expression: "", scope_cwd: null }])}
        >
          添加{verdict === "allow" ? "允许" : "拒绝"}规则
        </Button>
      </div>
      {rules.map((rule, index) => (
        <div
          className="space-y-2 rounded-md border border-input p-2"
          data-testid={`thread-permissions-${verdict}-rule`}
          key={`${verdict}-${index}`}
        >
          <label className="block space-y-1 text-xs">
            <span>行为表达式</span>
            <input
              aria-label={`${verdict} expression ${index + 1}`}
              className="w-full rounded-md border border-input bg-background px-2 py-1 font-mono"
              value={rule.expression}
              disabled={isSaving}
              onChange={(event) => {
                const next = [...rules];
                next[index] = { ...rule, expression: event.target.value };
                onChange(next);
              }}
            />
          </label>
          <label className="block space-y-1 text-xs">
            <span>目录作用域</span>
            <input
              aria-label={`${verdict} cwd ${index + 1}`}
              className="w-full rounded-md border border-input bg-background px-2 py-1 font-mono"
              placeholder={
                verdict === "deny" ? "留空表示当前 thread 全目录" : "非 Shell 规则留空"
              }
              value={rule.scope_cwd ?? ""}
              disabled={isSaving}
              onChange={(event) => {
                const next = [...rules];
                next[index] = {
                  ...rule,
                  scope_cwd: event.target.value || null,
                };
                onChange(next);
              }}
            />
          </label>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={isSaving}
            onClick={() => onChange(rules.filter((_, itemIndex) => itemIndex !== index))}
          >
            删除规则
          </Button>
        </div>
      ))}
    </section>
  );

  return (
    <section className="space-y-4" data-testid="thread-permissions-manager">
      <header className="space-y-1">
        <h3 className="text-base font-semibold">Thread 审批本子</h3>
        <p className="font-mono text-xs text-muted-foreground">{threadId}</p>
        <p className="text-xs text-muted-foreground" data-testid="thread-permissions-revision">
          revision {snapshot.revision}
        </p>
      </header>

      {isEmpty && (
        <p className="text-sm text-muted-foreground" data-testid="thread-permissions-empty">
          当前 thread 还没有记住的允许或拒绝规则。
        </p>
      )}

      {snapshot.migration_summary !== null && (
        <div
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
          data-testid="thread-permissions-migration-summary"
        >
          已升级审批本子；{snapshot.migration_summary.invalidated_shell_allow_count} 条旧
          Shell 允许规则已失效，下一次执行会重新审批。
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {renderRules("allow", allowDraft, onAllowDraftChange)}
        {renderRules("deny", denyDraft, onDenyDraftChange)}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" disabled={isSaving} onClick={() => void onSave()}>
          {isSaving ? "保存中…" : "保存审批本子"}
        </Button>
        {status === "saved" && (
          <span className="text-sm text-emerald-600" role="status">
            已保存
          </span>
        )}
        {status === "conflict" && (
          <>
            <span className="text-sm text-destructive" role="alert">
              revision 冲突：{errorMessage ?? "审批本子已被其他窗口更新"}
            </span>
            <Button type="button" variant="outline" onClick={() => void onReload()}>
              重新加载
            </Button>
          </>
        )}
        {status === "error" && !loadFailed && (
          <span className="text-sm text-destructive" role="alert">
            保存失败：{errorMessage ?? "未知错误"}
          </span>
        )}
      </div>
    </section>
  );
}
