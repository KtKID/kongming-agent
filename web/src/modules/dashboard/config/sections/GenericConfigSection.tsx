/**
 * dashboard/config 子模块内部通用 section。
 *
 * 用于后端 schema 已声明、前端暂无专用 section 的配置分组。组件按字段 path
 * 自动分桶：优先按当前 group 内的相对路径分组（如 sitian.analyzer），group
 * 直属字段进入基础配置桶，保证新增字段进入 UI 后有稳定展示入口。
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

const BUCKET_LABELS: Record<string, string> = {
  "__other__": "其他",
  "sitian.__general__": "基础配置",
  "sitian.analyzer": "分析器",
  "sitian.interests": "关注范围",
  "sitian.scanner": "扫描器",
  "sitian.sources": "来源",
  "workflow.__general__": "基础配置",
};

const BUCKET_ORDER: Record<string, number> = {
  "__other__": 99,
  "sitian.__general__": 0,
  "sitian.scanner": 1,
  "sitian.analyzer": 2,
  "sitian.interests": 3,
  "sitian.sources": 4,
  "workflow.__general__": 0,
};

function prettifyBucketKey(key: string): string {
  if (BUCKET_LABELS[key]) return BUCKET_LABELS[key];
  return key
    .split(".")
    .filter(Boolean)
    .map((part) => part.replaceAll("_", " "))
    .join(" / ");
}

function bucketKey(path: string, groupId: string): string {
  if (!path) return "__other__";
  const prefix = `${groupId}.`;
  if (path.startsWith(prefix)) {
    const relativeParts = path.slice(prefix.length).split(".").filter(Boolean);
    if (relativeParts.length >= 2) return `${groupId}.${relativeParts[0]}`;
    return `${groupId}.__general__`;
  }
  const parts = path.split(".").filter(Boolean);
  if (parts.length >= 2) return parts.slice(0, 2).join(".");
  return parts[0] ?? "__other__";
}

function bucketize(fields: FieldMeta[], groupId: string): FieldBucket[] {
  const buckets = new Map<string, FieldMeta[]>();
  for (const meta of fields) {
    const key = bucketKey(meta.path, groupId);
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.push(meta);
    } else {
      buckets.set(key, [meta]);
    }
  }
  return Array.from(buckets, ([key, bucketFields]) => ({
    key,
    label: prettifyBucketKey(key),
    fields: [...bucketFields].sort((a, b) => a.path.localeCompare(b.path)),
  })).sort((a, b) => {
    const orderDelta = (BUCKET_ORDER[a.key] ?? 50) - (BUCKET_ORDER[b.key] ?? 50);
    return orderDelta === 0 ? a.key.localeCompare(b.key) : orderDelta;
  });
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

  const buckets = bucketize(fields, groupId);

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
