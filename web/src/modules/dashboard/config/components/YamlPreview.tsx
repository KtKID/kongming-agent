/**
 * dashboard/config 子模块内部组件，**不要被 sibling 模块直接 import**。
 *
 * 折叠展示 yaml 原文 + 轻量手写正则高亮（不引入新依赖）。
 * 用于 ConfigPage 的 safety / 复杂段下方展示 `setting.yaml` 原始片段，
 * 让用户在表单编辑之外，仍能核对实际写盘的 yaml 结构。
 *
 * 设计取舍：
 * - 折叠用原生 `<details>/<summary>`：项目没有 Collapsible / Accordion shadcn 组件，
 *   原生标签在 a11y / 键盘交互上已经够用，零依赖。
 * - 高亮用 5 条正则行级 token 化（注释 / 键 / 字符串 / 数字 / 布尔常量）：
 *   - 不引入 prismjs / highlight.js（package.json 也没有），保持 bundle 轻量；
 *   - 行级 token 化避免破坏 yaml 缩进语义；
 *   - 配色全部走 Tailwind theme token，跟随深浅主题切换。
 * - 滚动用 `div.overflow-auto`：**不**用 Radix ScrollArea
 *   （ScrollArea 会注入 display:table 破坏布局，CLAUDE.md 明确禁用）。
 */

import { useId, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

export interface YamlPreviewProps {
  /** yaml 原文 */
  content: string;
  /** 是否默认展开，默认 false */
  defaultOpen?: boolean;
  /** 折叠标题，默认 "查看 yaml 原文" */
  title?: string;
  /** 最大高度（px），超出滚动，默认 480 */
  maxHeight?: number;
}

/** 行级 token，按出现顺序拼成 ReactNode 数组 */
interface Token {
  kind: "comment" | "key" | "string" | "number" | "bool" | "text";
  value: string;
}

/**
 * yaml 单行 token 化。
 *
 * 规则按"行内一次扫描 + 优先级"做：
 * 1. 整行注释（`#` 起头，前面只有空白）或行尾注释片段，独立成 token
 * 2. 行首 `<indent><key>:` 这种结构里 `key` 取 key 色
 * 3. 字符串字面量（单/双引号）识别为 string
 * 4. 整数 / 浮点数识别为 number
 * 5. true / false / null / yes / no / on / off 识别为 bool
 * 6. 其他剩余文本走默认色
 *
 * 不处理多行 block scalar（`|` / `>`）的 body —— 那种行直接当 text 渲染，
 * 不影响高亮正确性，只是颜色少一些；后续真出现需要再扩。
 */
function tokenizeLine(line: string): Token[] {
  const tokens: Token[] = [];
  let rest = line;

  // 1. 整行注释（允许前导空白）
  const fullComment = rest.match(/^(\s*)(#.*)$/);
  if (fullComment) {
    if (fullComment[1]) tokens.push({ kind: "text", value: fullComment[1] });
    tokens.push({ kind: "comment", value: fullComment[2] });
    return tokens;
  }

  // 2. 行尾注释（` #` 前必须有空白，避免误伤 `value#1` 这种字符串）
  let trailingComment = "";
  const trailingMatch = rest.match(/^(.*?)(\s+#.*)$/);
  if (trailingMatch) {
    rest = trailingMatch[1];
    trailingComment = trailingMatch[2];
  }

  // 3. 行首 key:（含 list item 形式 `- key:`）
  const keyMatch = rest.match(/^(\s*-?\s*)([A-Za-z_][\w.-]*)(\s*:)(.*)$/);
  if (keyMatch) {
    const [, indent, key, colon, after] = keyMatch;
    if (indent) tokens.push({ kind: "text", value: indent });
    tokens.push({ kind: "key", value: key });
    tokens.push({ kind: "text", value: colon });
    tokens.push(...tokenizeValuePart(after));
  } else {
    tokens.push(...tokenizeValuePart(rest));
  }

  if (trailingComment) {
    tokens.push({ kind: "comment", value: trailingComment });
  }
  return tokens;
}

/**
 * 对 yaml 行的"值部分"做二次扫描，识别字符串/数字/布尔。
 * 把剩下的散文本碎片归到 text token。
 */
function tokenizeValuePart(input: string): Token[] {
  if (!input) return [];
  const tokens: Token[] = [];
  // 一遍走完：字符串 / 数字 / 布尔关键字 / 其他
  const pattern =
    /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null|yes|no|on|off|True|False|Null|TRUE|FALSE|NULL)\b/g;

  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(input)) !== null) {
    if (match.index > cursor) {
      tokens.push({ kind: "text", value: input.slice(cursor, match.index) });
    }
    if (match[1] !== undefined) {
      tokens.push({ kind: "string", value: match[1] });
    } else if (match[2] !== undefined) {
      tokens.push({ kind: "number", value: match[2] });
    } else if (match[3] !== undefined) {
      tokens.push({ kind: "bool", value: match[3] });
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < input.length) {
    tokens.push({ kind: "text", value: input.slice(cursor) });
  }
  return tokens;
}

const TOKEN_CLASS: Record<Token["kind"], string> = {
  comment: "text-muted-foreground/80 italic",
  key: "text-sky-600 dark:text-sky-300",
  string: "text-emerald-600 dark:text-emerald-300",
  number: "text-amber-600 dark:text-amber-300",
  bool: "text-violet-600 dark:text-violet-300",
  text: "text-foreground",
};

function renderTokens(tokens: Token[], lineKey: number): ReactNode {
  return tokens.map((t, i) => (
    <span key={`${lineKey}-${i}`} className={TOKEN_CLASS[t.kind]}>
      {t.value}
    </span>
  ));
}

export function YamlPreview({
  content,
  defaultOpen = false,
  title = "查看 yaml 原文",
  maxHeight = 480,
}: YamlPreviewProps): ReactNode {
  const summaryId = useId();
  const lines = content.split("\n");

  return (
    <details
      className={cn(
        "group rounded-2xl border border-border/70 bg-card/75 shadow-sm",
      )}
      open={defaultOpen}
    >
      <summary
        id={summaryId}
        className={cn(
          "flex cursor-pointer select-none items-center gap-2 rounded-2xl px-4 py-2.5",
          "text-sm font-medium text-foreground",
          "hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          // 隐藏浏览器默认 ▶ 标记，自己用 lucide chevron
          "[&::-webkit-details-marker]:hidden [list-style:none]",
        )}
      >
        <ChevronRight
          className="h-4 w-4 text-muted-foreground transition-transform duration-150 group-open:rotate-90"
          aria-hidden
        />
        <span>{title}</span>
        <span className="ml-auto text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
          {lines.length} 行
        </span>
      </summary>
      <div
        className="overflow-auto border-t border-border/70 bg-muted/20 px-4 py-3"
        style={{ maxHeight }}
      >
        <pre className="font-mono text-xs leading-relaxed text-foreground">
          <code>
            {lines.map((line, idx) => (
              <div key={idx} className="whitespace-pre">
                {line.length === 0
                  ? " "
                  : renderTokens(tokenizeLine(line), idx)}
              </div>
            ))}
          </code>
        </pre>
      </div>
    </details>
  );
}
