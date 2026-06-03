/**
 * dashboard/config 子模块内部 section，**不要被 sibling 模块直接 import**，
 * 由 ConfigPage 装配后渲染。
 *
 * 渲染 runtime 组所有字段。runtime 字段数量多（约 17 个），按 `meta.path` 的
 * 第一段前缀分子桶（runner / session / compactor / retry / stream / cli / 其他），
 * 每个子桶一个小 h4，让用户视觉上不会被一长溜字段淹没。
 *
 * 分桶顺序与 setting.yaml 顶层 key 顺序一致；遇到未列出的前缀归入"其他"，
 * 保证不漏字段（schema 扩字段时无需改本文件）。
 *
 * 不在本文件 fetch / 修改 store 全局状态（除调用 setField / clearField）。
 */

import { type ReactNode } from "react";

import { FieldRenderer } from "../components/FieldRenderer";
import { useConfigStore } from "../store";
import type { FieldMeta } from "../types";

export interface RuntimeSectionProps {
  /** 已过滤为 group=runtime 的字段。 */
  fields: FieldMeta[];
}

/** 分桶顺序与 label；末位 `__other__` 是兜底桶。 */
const SUB_GROUPS: Array<{ prefix: string; label: string }> = [
  { prefix: "runner", label: "runner — 主循环" },
  { prefix: "session", label: "session — 会话存储" },
  { prefix: "compactor", label: "compactor — 历史压缩" },
  { prefix: "retry", label: "retry — 失败重试" },
  { prefix: "stream", label: "stream — 流式" },
  { prefix: "cli", label: "cli — 命令行交互" },
  { prefix: "__other__", label: "其他" },
];

function bucketize(fields: FieldMeta[]): Map<string, FieldMeta[]> {
  const buckets = new Map<string, FieldMeta[]>();
  for (const { prefix } of SUB_GROUPS) buckets.set(prefix, []);

  for (const meta of fields) {
    const head = meta.path.split(".", 1)[0] ?? "";
    const known = SUB_GROUPS.find((g) => g.prefix !== "__other__" && g.prefix === head);
    const key = known ? known.prefix : "__other__";
    buckets.get(key)!.push(meta);
  }
  return buckets;
}

export function RuntimeSection({ fields }: RuntimeSectionProps): ReactNode {
  const effective = useConfigStore((s) => s.effective);
  const dirty = useConfigStore((s) => s.dirty);
  const setField = useConfigStore((s) => s.setField);
  const clearField = useConfigStore((s) => s.clearField);

  if (!effective) return null;

  const buckets = bucketize(fields);

  return (
    <div className="space-y-5" data-section="runtime">
      <header className="space-y-1">
        <h3 className="text-base font-semibold text-foreground">运行时</h3>
        <p className="text-sm text-muted-foreground">
          主循环、会话存储、历史压缩、失败重试、流式与 CLI 交互参数。
        </p>
      </header>

      {SUB_GROUPS.map(({ prefix, label }) => {
        const sub = buckets.get(prefix) ?? [];
        if (sub.length === 0) return null;
        return (
          <section
            key={prefix}
            className="space-y-2"
            data-runtime-bucket={prefix}
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
