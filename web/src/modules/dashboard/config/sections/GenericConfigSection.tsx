/**
 * dashboard/config 子模块内部通用 section。
 *
 * 用于后端 schema 已声明、前端暂无专用 section 的配置分组。组件按字段 path
 * 自动分桶：三段及以上路径取前两段（如 sitian.analyzer），一到两段路径取
 * 第一段（如 workflow），保证新增字段进入 UI 后有稳定展示入口。
 */

import { type ReactNode } from "react";

import { FieldRenderer } from "../components/FieldRenderer";
import { useConfigStore } from "../store";
import type { FieldMeta } from "../types";

export interface GenericConfigSectionProps {
  groupId: string;
  label: string;
  fields: FieldMeta[];
}

interface FieldBucket {
  key: string;
  label: string;
  fields: FieldMeta[];
}

function bucketKey(path: string): string {
  if (!path) return "__other__";
  const parts = path.split(".");
  if (parts.length >= 3) return parts.slice(0, 2).join(".");
  return parts[0] ?? "__other__";
}

function bucketize(fields: FieldMeta[]): FieldBucket[] {
  const buckets = new Map<string, FieldMeta[]>();
  for (const meta of fields) {
    const key = bucketKey(meta.path);
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.push(meta);
    } else {
      buckets.set(key, [meta]);
    }
  }
  return Array.from(buckets, ([key, bucketFields]) => ({
    key,
    label: key === "__other__" ? "其他" : key,
    fields: bucketFields,
  }));
}

export function GenericConfigSection({
  groupId,
  label,
  fields,
}: GenericConfigSectionProps): ReactNode {
  const effective = useConfigStore((s) => s.effective);
  const dirty = useConfigStore((s) => s.dirty);
  const setField = useConfigStore((s) => s.setField);
  const clearField = useConfigStore((s) => s.clearField);

  if (!effective) return null;

  const buckets = bucketize(fields);

  return (
    <div className="space-y-5" data-section={groupId}>
      <header className="space-y-1">
        <h3 className="text-base font-semibold text-foreground">{label}</h3>
      </header>

      {buckets.map((bucket) => (
        <section
          key={bucket.key}
          className="space-y-2"
          data-generic-config-bucket={bucket.key}
        >
          <h4 className="text-sm font-semibold text-foreground/90">
            {bucket.label}
          </h4>
          <div className="space-y-3">
            {bucket.fields.map((meta) => {
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
      ))}
    </div>
  );
}
