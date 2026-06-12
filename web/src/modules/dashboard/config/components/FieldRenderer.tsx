/**
 * dashboard/config 子模块内部组件，**不要被 sibling 模块直接 import**。
 *
 * 按 `FieldMeta.type` 路由到对应 input 组件渲染单字段：
 * - string  → `<Input type="text">`
 * - int     → `<Input type="number" step="1">`（parseInt）
 * - float   → `<Input type="number" step="0.01">`（parseFloat）
 * - bool    → `<Switch>`（项目自带）
 * - enum    → 原生 `<select>` + 项目 token 样式（项目未装 shadcn Select，不引入新依赖）
 * - list/dict → ReadOnlyField（一期不可编辑，灰底显示 JSON）
 *
 * `editable=false` 一律走 ReadOnlyField；用户禁止 emoji，状态全部走纯文字 + 颜色 token。
 *
 * 数值校验：min/max 超出时 input 加红边框提示，但**不阻止用户输入**，
 * 让 store / 后端做最终校验（满足"严格类型 + 让 store/后端校验"的接口要求）。
 */

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

import type { FieldMeta, FieldSource } from "../types";

export interface FieldRendererProps {
  meta: FieldMeta;
  /** 当前值：脏字段为 dirty 值，否则为 effective 值 */
  value: unknown;
  source: FieldSource;
  isDirty: boolean;
  onChange: (value: unknown) => void;
  /** 撤销脏修改（仅 isDirty=true 时显示撤销按钮） */
  onClear: () => void;
}

// ─── source badge ─────────────────────────────────────────────────────────

const SOURCE_BADGE_CLASS: Record<FieldSource, string> = {
  yaml: "bg-blue-100 text-blue-700 dark:bg-blue-500/20 dark:text-blue-300",
  env: "bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300",
  default:
    "bg-muted text-muted-foreground dark:bg-muted/60 dark:text-muted-foreground",
};

const SOURCE_LABEL: Record<FieldSource, string> = {
  yaml: "yaml",
  env: "env",
  default: "default",
};

function SourceBadge({ source }: { source: FieldSource }): ReactNode {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide",
        SOURCE_BADGE_CLASS[source],
      )}
      data-source={source}
    >
      {SOURCE_LABEL[source]}
    </span>
  );
}

function DirtyBadge(): ReactNode {
  return (
    <span
      className="inline-flex items-center gap-1 text-[11px] font-medium text-red-600 dark:text-red-400"
      data-dirty="true"
    >
      <span className="inline-block h-1.5 w-1.5 rounded-full bg-red-500" />
      已改
    </span>
  );
}

// ─── sub-components ───────────────────────────────────────────────────────

function ReadOnlyField({
  meta,
  value,
}: {
  meta: FieldMeta;
  value: unknown;
}): ReactNode {
  const text =
    meta.type === "list" || meta.type === "dict"
      ? safeJsonStringify(value)
      : value === null || value === undefined
        ? ""
        : String(value);

  const multiline = meta.type === "list" || meta.type === "dict";
  return (
    <div className="space-y-1">
      <pre
        className={cn(
          "w-full rounded-md border border-border/70 bg-muted/40 px-3 py-2 font-mono text-xs text-muted-foreground",
          multiline ? "whitespace-pre overflow-auto" : "truncate",
        )}
        data-readonly="true"
      >
        {text || " "}
      </pre>
      {!meta.editable ? (
        <p className="text-[11px] text-muted-foreground">
          本字段不可在 UI 修改
        </p>
      ) : null}
    </div>
  );
}

function safeJsonStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function StringField({
  meta,
  value,
  onChange,
}: {
  meta: FieldMeta;
  value: unknown;
  onChange: (value: unknown) => void;
}): ReactNode {
  const str = value === null || value === undefined ? "" : String(value);
  return (
    <Input
      type="text"
      value={str}
      aria-label={meta.path}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function NumberField({
  meta,
  value,
  onChange,
  kind,
}: {
  meta: FieldMeta;
  value: unknown;
  onChange: (value: unknown) => void;
  kind: "int" | "float";
}): ReactNode {
  const raw =
    value === null || value === undefined || Number.isNaN(value as number)
      ? ""
      : String(value);
  const numeric = typeof value === "number" ? value : Number.NaN;
  const outOfRange =
    Number.isFinite(numeric) &&
    ((meta.min_value !== undefined && numeric < meta.min_value) ||
      (meta.max_value !== undefined && numeric > meta.max_value));

  return (
    <Input
      type="number"
      step={kind === "int" ? "1" : "0.01"}
      value={raw}
      aria-label={meta.path}
      aria-invalid={outOfRange || undefined}
      min={meta.min_value}
      max={meta.max_value}
      className={cn(
        outOfRange &&
          "border-red-500 focus-visible:ring-red-500 dark:border-red-400",
      )}
      onChange={(e) => {
        const text = e.target.value;
        if (text === "") {
          onChange(null);
          return;
        }
        const parsed = kind === "int" ? parseInt(text, 10) : parseFloat(text);
        // 解析失败仍把原字符串透回去，让用户能看到自己敲的内容；store/后端最终校验。
        onChange(Number.isNaN(parsed) ? text : parsed);
      }}
    />
  );
}

function BoolField({
  meta,
  value,
  onChange,
}: {
  meta: FieldMeta;
  value: unknown;
  onChange: (value: unknown) => void;
}): ReactNode {
  const checked = value === true;
  return (
    <div className="flex items-center gap-2">
      <Switch
        checked={checked}
        aria-label={meta.path}
        onCheckedChange={(next) => onChange(next)}
      />
      <span className="text-xs text-muted-foreground">
        {checked ? "true" : "false"}
      </span>
    </div>
  );
}

function EnumField({
  meta,
  value,
  onChange,
}: {
  meta: FieldMeta;
  value: unknown;
  onChange: (value: unknown) => void;
}): ReactNode {
  const options = meta.enum ?? [];
  const str = value === null || value === undefined ? "" : String(value);
  return (
    <select
      value={str}
      aria-label={meta.path}
      onChange={(e) => onChange(e.target.value)}
      className={cn(
        "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm",
        "transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
      )}
    >
      {options.map((opt) => (
        <option key={opt} value={opt}>
          {opt}
        </option>
      ))}
    </select>
  );
}

// ─── 主组件 ──────────────────────────────────────────────────────────────

export function FieldRenderer(props: FieldRendererProps): ReactNode {
  const { meta, value, source, isDirty, onChange, onClear } = props;

  const editor = renderEditor(meta, value, onChange);

  return (
    <div
      className={cn(
        "space-y-2 rounded-lg border border-border/70 bg-card/60 px-3 py-2.5",
      )}
      data-field-path={meta.path}
    >
      {/* 第 1 行：path + source badge + 脏标记 */}
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs text-foreground">{meta.path}</span>
        <SourceBadge source={source} />
        {isDirty ? <DirtyBadge /> : null}
        {meta.restart_required ? (
          <span className="ml-auto text-[10px] uppercase tracking-wide text-muted-foreground">
            需重启
          </span>
        ) : null}
      </div>

      {/* 第 2 行：desc */}
      {meta.desc ? (
        <p className="text-xs text-muted-foreground">{meta.desc}</p>
      ) : null}

      {/* 第 3 行：input + 撤销按钮 */}
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">{editor}</div>
        {isDirty ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onClear}
            aria-label={`撤销 ${meta.path}`}
          >
            撤销
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function renderEditor(
  meta: FieldMeta,
  value: unknown,
  onChange: (value: unknown) => void,
): ReactNode {
  // editable=false 或 list/dict 一律走只读
  if (!meta.editable || meta.type === "list" || meta.type === "dict") {
    return <ReadOnlyField meta={meta} value={value} />;
  }

  switch (meta.type) {
    case "string":
      return <StringField meta={meta} value={value} onChange={onChange} />;
    case "int":
      return (
        <NumberField meta={meta} value={value} onChange={onChange} kind="int" />
      );
    case "float":
      return (
        <NumberField
          meta={meta}
          value={value}
          onChange={onChange}
          kind="float"
        />
      );
    case "bool":
      return <BoolField meta={meta} value={value} onChange={onChange} />;
    case "enum":
      return <EnumField meta={meta} value={value} onChange={onChange} />;
    default:
      // 兜底：未知 type 当只读处理，避免崩溃
      return <ReadOnlyField meta={meta} value={value} />;
  }
}
