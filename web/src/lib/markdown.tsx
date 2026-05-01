import { Fragment, type ReactNode } from "react";

/**
 * kongming-agent v0.1.5 精简 markdown
 *
 * 支持：
 * - 代码块 ```lang\ncode\n```（lang 可选）
 * - 行内代码 `code`
 * - 链接 [text](url)
 * - 加粗 **text**
 * - 斜体 *text*
 * - 列表 `- item` / `1. item`
 * - 段落（空行分隔）
 *
 * 不支持：表格、引用块、HTML 嵌入、setext 标题。
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
  type: "p" | "code" | "ul" | "ol" | "blank";
  lang?: string;
  lines: string[];
}

/** 块级解析：把整段文本切成 BlockToken[] */
function parseBlocks(src: string): BlockToken[] {
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

    // 段落：连续非空行
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i]!.trim() !== "" &&
      !/^```/.test(lines[i]!) &&
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

export function Markdown({ text, className }: MarkdownProps) {
  const tokens = parseBlocks(text);
  let key = 0;
  return (
    <div className={className}>
      {tokens.map((tok) => {
        switch (tok.type) {
          case "blank":
            return null;
          case "code":
            return (
              <pre
                key={key++}
                className="my-2 overflow-x-auto rounded-md border border-border bg-muted p-3 text-xs"
              >
                <code className="font-mono">{tok.lines.join("\n")}</code>
              </pre>
            );
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
}
