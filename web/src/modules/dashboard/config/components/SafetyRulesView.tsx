/**
 * dashboard/config 子模块内部组件，**不要被 sibling 模块直接 import**。
 *
 * safety 段只读视图：把 `setting.yaml::safety` 的 8 个子段（7 个列表/列表-字符串 +
 * 1 个 bool 开关）渲染成 8 张 card，每张 card 顶部带"一期只读"提示。
 *
 * 设计取舍：
 * - **一期只读**：README + dev-checklist #13 明确"list 字段一期不可编辑，编辑能力
 *   留二期 task"；本组件不渲染任何 input / button / 编辑控件。
 * - **表格用原生 `<table>`**：项目无 shadcn Table 组件，原生标签 + Tailwind token
 *   够用，不引入新依赖；列宽按"name 短 / matcher 主 / reason 次"分布。
 * - **不用 Radix ScrollArea**：CLAUDE.md 明确禁用（ScrollArea 注入 display:table
 *   会破坏 truncate）。长 matcher / reason 直接 `break-all` + 列容器限宽。
 * - **yaml 折叠复用 `YamlPreview`**：抽出 `safety:` 段子串后整体丢给 YamlPreview，
 *   不重复实现高亮逻辑。
 * - **list 字段类型** narrow：`EffectiveResponse.values` 是 `Record<string, unknown>`
 *   扁平，list 字段值是整段数组（Python `model_dump()` 不再二次拆 path 进数组内部），
 *   用 `Array.isArray` + 单条 `narrowXxxRow()` 收口，未知形状的项渲染为占位文本，
 *   不抛错。
 *
 * 安全：所有用户内容都走 React 文本节点，**禁止** `dangerouslySetInnerHTML`。
 */

import { type ReactNode } from "react";

import { cn } from "@/lib/utils";

import type { EffectiveResponse, RawResponse } from "../types";

import { YamlPreview } from "./YamlPreview";

export interface SafetyRulesViewProps {
  /** store 加载的当前 effective 值（带 source 标注） */
  effective: EffectiveResponse;
  /** setting.yaml 原文（用于折叠 yaml 块抽 safety 段） */
  raw: RawResponse;
}

// ---------------------------------------------------------------------------
// 行类型 narrow helpers
// ---------------------------------------------------------------------------

/** EffectiveResponse.values 里 list 字段的原始项形状未知，用守卫安全取字段。 */
function pick(
  row: unknown,
  key: string,
): string | number | boolean | string[] | undefined {
  if (typeof row !== "object" || row === null) return undefined;
  const v = (row as Record<string, unknown>)[key];
  if (v === undefined || v === null) return undefined;
  if (
    typeof v === "string" ||
    typeof v === "number" ||
    typeof v === "boolean"
  ) {
    return v;
  }
  if (Array.isArray(v) && v.every((x) => typeof x === "string")) {
    return v as string[];
  }
  return undefined;
}

/** 把任意值显示为字符串；undefined / null 显示为 `—`。 */
function fmt(v: unknown): string {
  if (v === undefined || v === null) return "—";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

/** 安全取扁平 list 字段值（Python 端 model_dump 后保留嵌套结构）。 */
function asList(values: Record<string, unknown>, path: string): unknown[] {
  const v = values[path];
  return Array.isArray(v) ? v : [];
}

/** 安全取扁平 bool 字段值。 */
function asBool(values: Record<string, unknown>, path: string): boolean {
  return values[path] === true;
}

// ---------------------------------------------------------------------------
// extractSafetySection: 从 raw yaml 原文抽出 `safety:` 段
// ---------------------------------------------------------------------------

/**
 * 从整段 `setting.yaml` 原文里切出 `safety:` 段子串。
 *
 * 实现：
 * 1. 按行扫描，找到行首 `^safety:` 的起点（行号 sIdx）
 * 2. 从 sIdx+1 起继续扫，找下一个"顶层 key"（行首小写字母 / 下划线 / 数字开头，
 *    冒号收尾，无前导空白）的起点 eIdx
 * 3. 切片 lines[sIdx..eIdx) 拼回字符串返回
 *
 * 边角情况：
 * - 整篇没有 `safety:` 段 → 返回 `"（无 safety 段）"`（理论不可能，防御性）
 * - safety 是最后一段 → eIdx 走到末尾，整段返回
 * - safety: 行后紧跟注释 / 空行 → 一并保留（属于 safety 子树视觉一部分）
 *
 * 同文件 export 工具函数（被 vitest 直接消费）会触发
 * `react-refresh/only-export-components` warning，本文件不依赖 HMR 热刷新，
 * 抑制该规则比为单函数拆文件更克制。
 */
// eslint-disable-next-line react-refresh/only-export-components
export function extractSafetySection(content: string): string {
  if (!content) return "（无 safety 段）";
  const lines = content.split("\n");
  let sIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    if (/^safety\s*:/.test(lines[i])) {
      sIdx = i;
      break;
    }
  }
  if (sIdx < 0) return "（无 safety 段）";

  // 下一个顶层 key：行首非空白 + 字母 / 下划线 / 数字 + 任意符号 + `:`
  // 兼容注释行（# 起头）和空行不算"顶层 key"，继续往下扫
  let eIdx = lines.length;
  for (let i = sIdx + 1; i < lines.length; i++) {
    const line = lines[i];
    if (/^[A-Za-z_][\w.-]*\s*:/.test(line)) {
      eIdx = i;
      break;
    }
  }
  return lines.slice(sIdx, eIdx).join("\n");
}

// ---------------------------------------------------------------------------
// 单段渲染 helpers
// ---------------------------------------------------------------------------

const PANEL_BASE = cn(
  "rounded-2xl border border-border/70 bg-card/75 shadow-sm",
  "px-4 py-3 space-y-2",
);

const TABLE_HEAD = cn(
  "text-left text-[11px] uppercase tracking-[0.16em]",
  "text-muted-foreground font-medium pb-1.5",
);

const TABLE_CELL = cn(
  "align-top py-1.5 pr-3 text-xs text-foreground",
  "break-all", // matcher / reason 允许 wrap
);

const EMPTY_HINT = (
  <p className="text-xs text-muted-foreground italic">
    （无自定义规则，仅默认基线）
  </p>
);

const READONLY_HINT = (
  <p className="text-[11px] text-muted-foreground">
    一期只读，编辑能力在后续版本开放
  </p>
);

function PanelHeader({
  title,
  subPath,
  desc,
}: {
  title: string;
  subPath: string;
  desc: string;
}): ReactNode {
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline gap-2 flex-wrap">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        <code className="text-[11px] text-muted-foreground font-mono">
          {subPath}
        </code>
      </div>
      <p className="text-xs text-muted-foreground leading-relaxed">{desc}</p>
      {READONLY_HINT}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 各子段表格
// ---------------------------------------------------------------------------

function HardDenyTable({ rows }: { rows: unknown[] }): ReactNode {
  if (rows.length === 0) return EMPTY_HINT;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="border-b border-border/60">
            <th className={cn(TABLE_HEAD, "w-[16%]")}>name</th>
            <th className={cn(TABLE_HEAD, "w-[28%]")}>matcher</th>
            <th className={cn(TABLE_HEAD, "w-[14%]")}>match_mode</th>
            <th className={cn(TABLE_HEAD, "w-[12%]")}>boundary_scope</th>
            <th className={cn(TABLE_HEAD, "w-[30%]")}>reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-border/30 last:border-b-0">
              <td className={cn(TABLE_CELL, "font-medium")}>
                {fmt(pick(row, "name"))}
              </td>
              <td className={cn(TABLE_CELL, "font-mono")}>
                {fmt(pick(row, "matcher"))}
              </td>
              <td className={TABLE_CELL}>{fmt(pick(row, "match_mode"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "boundary_scope"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "reason"))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ApprovalRequiredTable({ rows }: { rows: unknown[] }): ReactNode {
  if (rows.length === 0) return EMPTY_HINT;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="border-b border-border/60">
            <th className={cn(TABLE_HEAD, "w-[14%]")}>name</th>
            <th className={cn(TABLE_HEAD, "w-[24%]")}>matcher</th>
            <th className={cn(TABLE_HEAD, "w-[12%]")}>match_mode</th>
            <th className={cn(TABLE_HEAD, "w-[10%]")}>severity</th>
            <th className={cn(TABLE_HEAD, "w-[12%]")}>boundary_scope</th>
            <th className={cn(TABLE_HEAD, "w-[28%]")}>reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-border/30 last:border-b-0">
              <td className={cn(TABLE_CELL, "font-medium")}>
                {fmt(pick(row, "name"))}
              </td>
              <td className={cn(TABLE_CELL, "font-mono")}>
                {fmt(pick(row, "matcher"))}
              </td>
              <td className={TABLE_CELL}>{fmt(pick(row, "match_mode"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "severity"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "boundary_scope"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "reason"))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SensitivePathsTable({ rows }: { rows: unknown[] }): ReactNode {
  if (rows.length === 0) return EMPTY_HINT;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="border-b border-border/60">
            <th className={cn(TABLE_HEAD, "w-[14%]")}>name</th>
            <th className={cn(TABLE_HEAD, "w-[22%]")}>matcher</th>
            <th className={cn(TABLE_HEAD, "w-[12%]")}>match_mode</th>
            <th className={cn(TABLE_HEAD, "w-[12%]")}>ops</th>
            <th className={cn(TABLE_HEAD, "w-[10%]")}>effect</th>
            <th className={cn(TABLE_HEAD, "w-[10%]")}>boundary_scope</th>
            <th className={cn(TABLE_HEAD, "w-[20%]")}>reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-border/30 last:border-b-0">
              <td className={cn(TABLE_CELL, "font-medium")}>
                {fmt(pick(row, "name"))}
              </td>
              <td className={cn(TABLE_CELL, "font-mono")}>
                {fmt(pick(row, "matcher"))}
              </td>
              <td className={TABLE_CELL}>{fmt(pick(row, "match_mode"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "ops"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "effect"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "boundary_scope"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "reason"))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SkillCallRulesTable({ rows }: { rows: unknown[] }): ReactNode {
  if (rows.length === 0) return EMPTY_HINT;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="border-b border-border/60">
            <th className={cn(TABLE_HEAD, "w-[16%]")}>name</th>
            <th className={cn(TABLE_HEAD, "w-[24%]")}>matcher</th>
            <th className={cn(TABLE_HEAD, "w-[12%]")}>match_mode</th>
            <th className={cn(TABLE_HEAD, "w-[12%]")}>effect</th>
            <th className={cn(TABLE_HEAD, "w-[12%]")}>boundary_scope</th>
            <th className={cn(TABLE_HEAD, "w-[24%]")}>reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-border/30 last:border-b-0">
              <td className={cn(TABLE_CELL, "font-medium")}>
                {fmt(pick(row, "name"))}
              </td>
              <td className={cn(TABLE_CELL, "font-mono")}>
                {fmt(pick(row, "matcher"))}
              </td>
              <td className={TABLE_CELL}>{fmt(pick(row, "match_mode"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "effect"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "boundary_scope"))}</td>
              <td className={TABLE_CELL}>{fmt(pick(row, "reason"))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 通用：list[str] → 路径/名称列表 */
function StringListView({
  rows,
  label,
}: {
  rows: unknown[];
  label: string;
}): ReactNode {
  if (rows.length === 0) return EMPTY_HINT;
  return (
    <ul className="space-y-1">
      {rows.map((item, i) => (
        <li
          key={i}
          className={cn(
            "text-xs font-mono text-foreground break-all",
            "rounded-md bg-muted/40 px-2.5 py-1",
          )}
          aria-label={label}
        >
          {fmt(item)}
        </li>
      ))}
    </ul>
  );
}

/** log_silent_reads：单值 bool，渲染为不可点击的开关样式 */
function LogSilentReadsRow({ value }: { value: boolean }): ReactNode {
  return (
    <div className="flex items-center gap-3">
      <span
        role="switch"
        aria-checked={value}
        aria-disabled
        className={cn(
          "inline-flex h-5 w-9 items-center rounded-full px-0.5 transition-colors",
          value ? "bg-emerald-500/80" : "bg-muted-foreground/30",
          "cursor-not-allowed opacity-90",
        )}
      >
        <span
          className={cn(
            "h-4 w-4 rounded-full bg-background shadow",
            "transition-transform",
            value ? "translate-x-4" : "translate-x-0",
          )}
        />
      </span>
      <span className="text-xs text-foreground">
        {value ? "已开启（silent + read 写 trace）" : "已关闭（默认，防 jsonl 膨胀）"}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export function SafetyRulesView({
  effective,
  raw,
}: SafetyRulesViewProps): ReactNode {
  const values = effective.values;

  const hardDeny = asList(values, "safety.hard_deny_commands");
  const approval = asList(values, "safety.approval_required_commands");
  const sensitivePaths = asList(values, "safety.sensitive_paths");
  const skillRules = asList(values, "safety.skill_call_rules");
  const trustedDirs = asList(values, "safety.trusted_workdirs");
  const allowWrites = asList(values, "safety.allow_writes");
  const allowToolsSilent = asList(values, "safety.allow_tools_silent");
  const logSilentReads = asBool(values, "safety.log_silent_reads");

  return (
    <div className="space-y-4">
      <section className={PANEL_BASE} aria-label="hard_deny_commands">
        <PanelHeader
          title="命令硬禁"
          subPath="safety.hard_deny_commands"
          desc="命中即拒，不弹批准框；用于绝对不允许的高危命令。"
        />
        <HardDenyTable rows={hardDeny} />
      </section>

      <section className={PANEL_BASE} aria-label="approval_required_commands">
        <PanelHeader
          title="命令需审批"
          subPath="safety.approval_required_commands"
          desc="命中需要弹审批框确认；severity=elevated 触发更严格的提示。"
        />
        <ApprovalRequiredTable rows={approval} />
      </section>

      <section className={PANEL_BASE} aria-label="sensitive_paths">
        <PanelHeader
          title="路径风险规则"
          subPath="safety.sensitive_paths"
          desc="按读 / 写 / 执行 操作维度对路径做 block / elevated / allow 决策。"
        />
        <SensitivePathsTable rows={sensitivePaths} />
      </section>

      <section className={PANEL_BASE} aria-label="skill_call_rules">
        <PanelHeader
          title="Skill 调用规则"
          subPath="safety.skill_call_rules"
          desc="对 SkillTool 调用做精确名 / 前缀匹配，控制单条 skill 的 block / elevated / allow。"
        />
        <SkillCallRulesTable rows={skillRules} />
      </section>

      <section className={PANEL_BASE} aria-label="trusted_workdirs">
        <PanelHeader
          title="可信工作目录"
          subPath="safety.trusted_workdirs"
          desc="项目相对路径白名单：处于这些目录的操作走默认作用域。"
        />
        <StringListView rows={trustedDirs} label="trusted_workdir" />
      </section>

      <section className={PANEL_BASE} aria-label="allow_writes">
        <PanelHeader
          title="静默放行写路径"
          subPath="safety.allow_writes"
          desc="字符串字面前缀比对（绝对路径）；命中即 silent_allow 写操作。"
        />
        <StringListView rows={allowWrites} label="allow_write" />
      </section>

      <section className={PANEL_BASE} aria-label="allow_tools_silent">
        <PanelHeader
          title="静默放行工具"
          subPath="safety.allow_tools_silent"
          desc="工具名集合：命中后该工具调用走 silent_allow，不弹审批。"
        />
        <StringListView rows={allowToolsSilent} label="allow_tool" />
      </section>

      <section className={PANEL_BASE} aria-label="log_silent_reads">
        <PanelHeader
          title="silent + read 写 trace"
          subPath="safety.log_silent_reads"
          desc="是否把 silent_allow + read 类工具事件写入 trace；默认关闭以防 jsonl 膨胀。重启后生效。"
        />
        <LogSilentReadsRow value={logSilentReads} />
      </section>

      <YamlPreview
        content={extractSafetySection(raw.content)}
        title="safety 段 yaml 原文"
      />
    </div>
  );
}
