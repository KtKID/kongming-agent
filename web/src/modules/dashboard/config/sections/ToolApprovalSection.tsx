/**
 * dashboard/config 子模块内部 section，**不要被 sibling 模块直接 import**，
 * 由 ConfigPage 装配后渲染。
 *
 * 渲染 tool_approval 组所有字段，按 `meta.path` 第一段前缀分子桶：
 * tool / approval / scheduler / 兜底"其他"。
 *
 * 分桶逻辑与 RuntimeSection 同源，但桶定义不同；没抽成 util 是因为分桶配置就是
 * 这两个 section 的"业务"，抽成共用 utility 会让 sibling 改一个字段时不得不
 * 跨文件追踪，得不偿失。
 *
 * 不在本文件 fetch / 修改 store 全局状态（除调用 setField / clearField）。
 */

import { type ReactNode } from "react";

import { FieldRenderer } from "../components/FieldRenderer";
import { useConfigStore } from "../store";
import type { FieldMeta } from "../types";

export interface ToolApprovalSectionProps {
  fields: FieldMeta[];
}

const SUB_GROUPS: Array<{ prefix: string; label: string }> = [
  { prefix: "tool", label: "tool — 工具开关与白名单" },
  { prefix: "approval", label: "approval — 人审策略" },
  { prefix: "scheduler", label: "scheduler — cron 调度" },
  { prefix: "__other__", label: "其他" },
];

function bucketize(fields: FieldMeta[]): Map<string, FieldMeta[]> {
  const buckets = new Map<string, FieldMeta[]>();
  for (const { prefix } of SUB_GROUPS) buckets.set(prefix, []);

  for (const meta of fields) {
    const head = meta.path.split(".", 1)[0] ?? "";
    const known = SUB_GROUPS.find(
      (g) => g.prefix !== "__other__" && g.prefix === head,
    );
    const key = known ? known.prefix : "__other__";
    buckets.get(key)!.push(meta);
  }
  return buckets;
}

export function ToolApprovalSection({
  fields,
}: ToolApprovalSectionProps): ReactNode {
  const effective = useConfigStore((s) => s.effective);
  const dirty = useConfigStore((s) => s.dirty);
  const setField = useConfigStore((s) => s.setField);
  const clearField = useConfigStore((s) => s.clearField);

  if (!effective) return null;

  const buckets = bucketize(fields);

  return (
    <div className="space-y-5" data-section="tool_approval">
      <header className="space-y-1">
        <h3 className="text-base font-semibold text-foreground">工具与审批</h3>
        <p className="text-sm text-muted-foreground">
          工具开关、审批策略、cron 调度三类参数；改动多数需重启生效。
        </p>
      </header>

      {SUB_GROUPS.map(({ prefix, label }) => {
        const sub = buckets.get(prefix) ?? [];
        if (sub.length === 0) return null;
        return (
          <section
            key={prefix}
            className="space-y-2"
            data-toolapproval-bucket={prefix}
          >
            <h4 className="text-sm font-semibold text-foreground/90">
              {label}
            </h4>
            <div className="space-y-3">
              {sub.map((meta) => {
                const value =
                  meta.path in dirty
                    ? dirty[meta.path]
                    : effective.values[meta.path];
                const source = effective.sources[meta.path] ?? "default";
                const isDirty = meta.path in dirty;
                return (
                  <FieldRenderer
                    key={meta.path}
                    meta={meta}
                    value={value}
                    source={source}
                    isDirty={isDirty}
                    onChange={(v) => setField(meta.path, v)}
                    onClear={() => clearField(meta.path)}
                  />
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
  );
}
