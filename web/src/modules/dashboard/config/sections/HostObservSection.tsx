/**
 * dashboard/config 子模块内部 section，**不要被 sibling 模块直接 import**，
 * 由 ConfigPage 装配后渲染。
 *
 * 渲染 host_observ 组所有字段（host / web / trace / logging / evolution 五块），
 * 按 `meta.path` 第一段前缀分子桶。逻辑同 RuntimeSection / ToolApprovalSection。
 *
 * `web.llm_presets` 是 list 字段，会被 FieldRenderer 自动走 ReadOnly 分支
 * （以 JSON 形式展示），符合一期 list 不可编辑约定。
 *
 * 不在本文件 fetch / 修改 store 全局状态（除调用 setField / clearField）。
 */

import { type ReactNode } from "react";

import { FieldRenderer } from "../components/FieldRenderer";
import { useConfigStore } from "../store";
import type { FieldMeta } from "../types";

export interface HostObservSectionProps {
  fields: FieldMeta[];
}

const SUB_GROUPS: Array<{ prefix: string; label: string }> = [
  { prefix: "host", label: "host — 宿主装配" },
  { prefix: "web", label: "web — Web 服务" },
  { prefix: "trace", label: "trace — 事件落盘" },
  { prefix: "logging", label: "logging — 日志" },
  { prefix: "evolution", label: "evolution — 自我进化" },
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

export function HostObservSection({
  fields,
}: HostObservSectionProps): ReactNode {
  const effective = useConfigStore((s) => s.effective);
  const dirty = useConfigStore((s) => s.dirty);
  const setField = useConfigStore((s) => s.setField);
  const clearField = useConfigStore((s) => s.clearField);

  if (!effective) return null;

  const buckets = bucketize(fields);

  return (
    <div className="space-y-5" data-section="host_observ">
      <header className="space-y-1">
        <h3 className="text-base font-semibold text-foreground">宿主与可观测</h3>
        <p className="text-sm text-muted-foreground">
          宿主装配、Web 服务、事件 trace、日志与进化产物路径。
        </p>
      </header>

      {SUB_GROUPS.map(({ prefix, label }) => {
        const sub = buckets.get(prefix) ?? [];
        if (sub.length === 0) return null;
        return (
          <section
            key={prefix}
            className="space-y-2"
            data-hostobserv-bucket={prefix}
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
