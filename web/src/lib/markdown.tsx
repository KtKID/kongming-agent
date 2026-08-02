import { Fragment, Suspense, lazy, memo, useMemo, type ReactNode } from "react";

// mermaid 走独立 chunk（vite.config manualChunks），按需异步加载
const MermaidBlock = lazy(() => import("@/components/MermaidBlock"));

/**
 * kongming-agent v0.1.5 精简 markdown
 *
 * 支持：
 * - 标题 # ~ ###### (h1-h6)
 * - 引用块 > text
 * - 水平分割线 --- / *** / ___
 * - 代码块 ```lang\ncode\n```（lang 可选，lang===mermaid 时走 MermaidBlock 渲染 SVG）
 * - 行内代码 `code`
 * - 链接 [text](url)
 * - 加粗 **text**
 * - 斜体 *text*
 * - 列表 `- item` / `1. item`
 * - 段落（空行分隔）
 *
 * 不支持：表格、HTML 嵌入、setext 标题。
 *
 * ## XSS 防御
 *
 * **始终用 React 文本节点输出**——不用 dangerouslySetInnerHTML。
 * React 默认转义所有文本，`<script>` 会变成字面量字符串。
 * 链接的 `href` 用 React 标准 attribute，浏览器对 `javascript:` 的拦截由
 * 我们的白名单 `safeHref()` 兜底。
 */

/** 仅放行 http(s):// / mailto: / 相对路径；其它一律置换为 `#` */
function safeHref(href: string): string {
  const trimmed = href.trim();
  if (!trimmed) return "#";
  if (trimmed.startsWith("#") || trimmed.startsWith("/")) return trimmed;
  if (/^(https?|mailto):/i.test(trimmed)) return trimmed;
  return "#";
}

/** 行内片段解析：粗体 / 斜体 / 行内 code / 链接，返回 ReactNode[] */
function parseInline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let key = 0;

  // 优先级：行内 code > 链接 > 粗体 > 斜体
  while (rest.length > 0) {
    // 行内 code `xxx`
    const codeMatch = /^`([^`]+)`/.exec(rest);
    if (codeMatch) {
      out.push(
        <code
          key={key++}
          className="rounded bg-muted px-1 py-0.5 text-[0.875em] font-mono"
        >
          {codeMatch[1]}
        </code>,
      );
      rest = rest.slice(codeMatch[0].length);
      continue;
    }
    // 链接 [text](url)
    const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)/.exec(rest);
    if (linkMatch) {
      out.push(
        <a
          key={key++}
          href={safeHref(linkMatch[2]!)}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent underline-offset-4 hover:underline"
        >
          {linkMatch[1]}
        </a>,
      );
      rest = rest.slice(linkMatch[0].length);
      continue;
    }
    // 粗体 **text**
    const boldMatch = /^\*\*([^*]+)\*\*/.exec(rest);
    if (boldMatch) {
      out.push(<strong key={key++}>{boldMatch[1]}</strong>);
      rest = rest.slice(boldMatch[0].length);
      continue;
    }
    // 斜体 *text*
    const itMatch = /^\*([^*]+)\*/.exec(rest);
    if (itMatch) {
      out.push(<em key={key++}>{itMatch[1]}</em>);
      rest = rest.slice(itMatch[0].length);
      continue;
    }
    // 普通字符：吃到下一个特殊字符
    const next = rest.search(/[`*[]/);
    if (next === -1) {
      out.push(rest);
      break;
    } else if (next === 0) {
      // 落单的特殊字符，按字面量
      out.push(rest[0]);
      rest = rest.slice(1);
    } else {
      out.push(rest.slice(0, next));
      rest = rest.slice(next);
    }
  }
  return out;
}

interface BlockToken {
  type: "p" | "code" | "ul" | "ol" | "blank" | "heading" | "blockquote" | "hr";
  lang?: string;
  level?: 1 | 2 | 3 | 4 | 5 | 6;
  lines: string[];
}

/** 块级解析：把整段文本切成 BlockToken[]。导出供渲染性能测试 spy。 */
// eslint-disable-next-line react-refresh/only-export-components -- parser test seam
export function parseBlocks(src: string): BlockToken[] {
  const lines = src.split(/\r?\n/);
  const tokens: BlockToken[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i]!;

    // 代码块 ```lang
    const fence = /^```(\w*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1] ?? "";
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i]!)) {
        codeLines.push(lines[i]!);
        i++;
      }
      // 跳过 close fence
      if (i < lines.length) i++;
      tokens.push({ type: "code", lang, lines: codeLines });
      continue;
    }

    // 标题 # ~ ######
    const heading = /^(#{1,6})\s+(.+?)\s*$/.exec(line);
    if (heading) {
      tokens.push({
        type: "heading",
        level: heading[1]!.length as 1 | 2 | 3 | 4 | 5 | 6,
        lines: [heading[2]!],
      });
      i++;
      continue;
    }

    // 水平分割线 ---  ***  ___
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      tokens.push({ type: "hr", lines: [] });
      i++;
      continue;
    }

    // 引用块 > text
    if (/^\s*>\s?/.test(line)) {
      const quoteLines: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i]!)) {
        quoteLines.push(lines[i]!.replace(/^\s*>\s?/, ""));
        i++;
      }
      tokens.push({ type: "blockquote", lines: quoteLines });
      continue;
    }

    // 列表
    if (/^\s*[-*]\s+/.test(line)) {
      const ulLines: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i]!)) {
        ulLines.push(lines[i]!.replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      tokens.push({ type: "ul", lines: ulLines });
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const olLines: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i]!)) {
        olLines.push(lines[i]!.replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      tokens.push({ type: "ol", lines: olLines });
      continue;
    }

    // 空行
    if (line.trim() === "") {
      tokens.push({ type: "blank", lines: [] });
      i++;
      continue;
    }

    // 段落：连续非空行（遇到其它块级语法时停止）
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i]!.trim() !== "" &&
      !/^```/.test(lines[i]!) &&
      !/^#{1,6}\s+/.test(lines[i]!) &&
      !/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(lines[i]!) &&
      !/^\s*>\s?/.test(lines[i]!) &&
      !/^\s*[-*]\s+/.test(lines[i]!) &&
      !/^\s*\d+\.\s+/.test(lines[i]!)
    ) {
      paraLines.push(lines[i]!);
      i++;
    }
    tokens.push({ type: "p", lines: paraLines });
  }
  return tokens;
}

interface MarkdownProps {
  text: string;
  className?: string;
}

let markdownParser: typeof parseBlocks = parseBlocks;

/** 测试可替换 parser，以验证流式纯文本路径和按 text 的 memoized 解析次数。 */
// eslint-disable-next-line react-refresh/only-export-components -- parser test seam
export function __setMarkdownParserForTest(parser: typeof parseBlocks | null): void {
  markdownParser = parser ?? parseBlocks;
}

export const Markdown = memo(function Markdown({ text, className }: MarkdownProps) {
  const tokens = useMemo(() => markdownParser(text), [text]);
  let key = 0;
  return (
    <div className={className}>
      {tokens.map((tok) => {
        switch (tok.type) {
          case "blank":
            return null;
          case "heading": {
            const HeadingTag = `h${tok.level ?? 1}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
            const headingClass: Record<number, string> = {
              1: "mt-3 mb-1 text-lg font-semibold leading-snug",
              2: "mt-3 mb-1 text-base font-semibold leading-snug",
              3: "mt-2 mb-1 text-sm font-semibold leading-snug",
              4: "mt-2 mb-1 text-sm font-medium leading-snug",
              5: "mt-2 mb-1 text-xs font-medium leading-snug text-muted-foreground",
              6: "mt-2 mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground",
            };
            return (
              <HeadingTag key={key++} className={headingClass[tok.level ?? 1]}>
                {parseInline(tok.lines[0] ?? "")}
              </HeadingTag>
            );
          }
          case "hr":
            return (
              <hr
                key={key++}
                className="my-3 border-0 border-t border-border"
              />
            );
          case "blockquote":
            return (
              <blockquote
                key={key++}
                className="my-2 border-l-[3px] border-accent rounded-r-md bg-accent/10 px-3 py-2 text-sm text-muted-foreground"
              >
                {tok.lines.map((line, i) => (
                  <Fragment key={i}>
                    {i > 0 ? <br /> : null}
                    {parseInline(line)}
                  </Fragment>
                ))}
              </blockquote>
            );
          case "code": {
            const source = tok.lines.join("\n");
            if (tok.lang === "mermaid") {
              const fallback = (
                <pre className="my-2 overflow-x-auto rounded-md border border-border bg-muted p-3 text-xs">
                  <code className="font-mono">{source}</code>
                </pre>
              );
              return (
                <Suspense key={key++} fallback={fallback}>
                  <MermaidBlock source={source} />
                </Suspense>
              );
            }
            return (
              <pre
                key={key++}
                className="my-2 overflow-x-auto rounded-md border border-border bg-muted p-3 text-xs"
              >
                <code className="font-mono">{source}</code>
              </pre>
            );
          }
          case "ul":
            return (
              <ul
                key={key++}
                className="my-2 list-disc space-y-0.5 pl-6 text-sm"
              >
                {tok.lines.map((line, i) => (
                  <li key={i}>{parseInline(line)}</li>
                ))}
              </ul>
            );
          case "ol":
            return (
              <ol
                key={key++}
                className="my-2 list-decimal space-y-0.5 pl-6 text-sm"
              >
                {tok.lines.map((line, i) => (
                  <li key={i}>{parseInline(line)}</li>
                ))}
              </ol>
            );
          case "p":
            return (
              <p key={key++} className="my-2 leading-relaxed text-sm">
                {tok.lines.map((line, i) => (
                  <Fragment key={i}>
                    {i > 0 ? <br /> : null}
                    {parseInline(line)}
                  </Fragment>
                ))}
              </p>
            );
          default:
            return null;
        }
      })}
    </div>
  );
});
