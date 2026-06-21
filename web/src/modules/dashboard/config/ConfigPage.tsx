/**
 * dashboard/config 子模块**顶级容器**。由路由层 `/manage/config/:section`
 * 直接挂载，本身就是路由组件——不暴露给非路由调用方。
 *
 * ## 职责
 *
 * - 启动加载：根据 `loadStatus === "idle"` 触发一次 `store.load()`
 *   （schema + effective + raw 三个 fetch 并发，store 内部处理错误归一化）
 * - 顶部横向 group tab：用 `NavLink` 同步 URL（`/manage/config/:section`）
 *   - 视觉与 `pages/Manage.tsx` 左侧竖 tab 同款 token（`bg-primary/12` /
 *     `text-foreground` 选中，`hover:bg-secondary` 悬停）
 * - 中间区域：按 `:section` 渲染对应 Section 组件
 * - 底部：SaveRestartBar（sticky 在容器底部，不在 viewport 底部）
 *
 * ## section 兜底重定向
 *
 * - 无 `:section` → 重定向到 `/manage/config/model`
 * - `:section` 不在后端 schema.groups 中 → 同上
 * - 重定向用 `Navigate replace`，避免污染浏览器历史
 *
 * ## SafetySection 签名不一致
 *
 * SafetySection 一期不接受 `fields` prop（safety 字段全部是 list/dict，
 * 全量只读由 SafetyRulesView 接管），其它专用 section 都按 `{ fields }` 接收。
 * 未专门实现的后端 group 走 GenericConfigSection 兜底。
 *
 * ## 数据流不直连 fetch
 *
 * 所有数据通过 `useConfigStore` 拿（schema / effective / raw / loadStatus）。
 * 本文件**不直接 import** `./api`，遵守"sibling 组件只过 store" 的边界约定。
 */

import { useEffect, type ReactNode } from "react";
import { Navigate, NavLink, useParams } from "react-router-dom";

import { cn } from "@/lib/utils";

import { SaveRestartBar } from "./components/SaveRestartBar";
import { GenericConfigSection } from "./sections/GenericConfigSection";
import { HostObservSection } from "./sections/HostObservSection";
import { ModelSection } from "./sections/ModelSection";
import { RuntimeSection } from "./sections/RuntimeSection";
import { SafetySection } from "./sections/SafetySection";
import { ToolApprovalSection } from "./sections/ToolApprovalSection";
import { useConfigStore } from "./store";
import type { FieldMeta } from "./types";

/**
 * 渲染指定 section。已实现专用 UI 的 group 走专用组件，其它后端 group 走通用组件。
 */
function renderSection(section: string, label: string, fields: FieldMeta[]): ReactNode {
  switch (section) {
    case "model":
      return <ModelSection fields={fields} />;
    case "runtime":
      return <RuntimeSection fields={fields} />;
    case "tool_approval":
      return <ToolApprovalSection fields={fields} />;
    case "host_observ":
      return <HostObservSection fields={fields} />;
    case "safety":
      return <SafetySection />;
    default:
      return (
        <GenericConfigSection groupId={section} label={label} fields={fields} />
      );
  }
}

export function ConfigPage(): ReactNode {
  const { section } = useParams<{ section?: string }>();
  const schema = useConfigStore((s) => s.schema);
  const loadStatus = useConfigStore((s) => s.loadStatus);
  const loadError = useConfigStore((s) => s.loadError);
  const load = useConfigStore((s) => s.load);

  // 启动加载：仅 idle 时触发一次，避免 Strict Mode / 切 tab 重复 fetch
  useEffect(() => {
    if (loadStatus === "idle") {
      void load();
    }
  }, [loadStatus, load]);

  // 无 section 时先落到 model；加载完成后再按后端 groups 校验动态 section
  if (!section) {
    return <Navigate to="/manage/config/model" replace />;
  }

  if (loadStatus === "loading") {
    return (
      <div
        className="p-6 text-sm text-muted-foreground"
        data-testid="config-loading"
      >
        加载配置中...
      </div>
    );
  }

  if (loadStatus === "error") {
    return (
      <div
        className="p-6 text-sm text-destructive"
        data-testid="config-error"
      >
        加载失败：{loadError ?? "未知错误"}
      </div>
    );
  }

  if (!schema) return null;

  const group = schema.groups.find((g) => g.id === section);
  if (schema.groups.length > 0 && !group) {
    const fallback = schema.groups[0]?.id ?? "model";
    return <Navigate to={`/manage/config/${fallback}`} replace />;
  }

  const fields = schema.fields.filter((f) => f.group === section);
  const sectionLabel = group?.label ?? section;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="config-page">
      {/* 顶部横向 group tab —— groups 为空时不渲染整个 tab 行 */}
      {schema.groups.length > 0 ? (
        <nav
          role="tablist"
          aria-label="配置分组"
          className="mb-4 flex flex-wrap gap-1 rounded-2xl border border-border/70 bg-card/70 p-1 shadow-sm"
          data-testid="config-section-tabs"
        >
          {schema.groups.map((g) => (
            <NavLink
              key={g.id}
              to={`/manage/config/${g.id}`}
              role="tab"
              className={({ isActive }) =>
                cn(
                  "rounded-xl px-3 py-2 text-xs font-medium transition-colors",
                  isActive
                    ? "bg-primary/12 text-foreground"
                    : "text-muted-foreground hover:bg-secondary hover:text-foreground",
                )
              }
            >
              {g.label}
            </NavLink>
          ))}
        </nav>
      ) : null}

      {/* 当前 section —— 独立滚动区，避免和 sticky bar 干扰 */}
      <div className="min-h-0 flex-1 overflow-y-auto pb-4">
        {renderSection(section, sectionLabel, fields)}
      </div>

      {/* 底部固定操作栏（SaveRestartBar 内部已 sticky） */}
      <SaveRestartBar />
    </div>
  );
}
