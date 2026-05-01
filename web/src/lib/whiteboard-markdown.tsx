import { Fragment, type ReactNode } from "react";

type Token =
  | { type: "blank" }
  | { type: "hr" }
  | { type: "code"; lang?: string; lines: string[] }
  | { type: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; text: string }
  | { type: "blockquote"; lines: string[] }
  | { type: "ul"; items: string[] }
  | { type: "ol"; items: string[] }
  | { type: "p"; lines: string[] };

interface WhiteboardMarkdownProps {
  text: string;
  className?: string;
  onToggleTask?: (taskIndex: number) => void;
}

const TASK_LINE_RE = /^(\s*[-*]\s+\[)( |x|X)(\]\s+.*)$/;

function safeHref(href: string): string {
  const trimmed = href.trim();
  if (!trimmed) return "#";
  if (trimmed.startsWith("#") || trimmed.startsWith("/")) return trimmed;
  if (/^(https?|mailto):/i.test(trimmed)) return trimmed;
  return "#";
}

function parseInline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let key = 0;

  while (rest.length > 0) {
    const codeMatch = /^`([^`]+)`/.exec(rest);
    if (codeMatch) {
      out.push(
        <code
          key={key++}
          className="rounded bg-stone-100 px-1 py-0.5 font-mono text-[0.82em] text-stone-900"
        >
          {codeMatch[1]}
        </code>,
      );
      rest = rest.slice(codeMatch[0].length);
      continue;
    }

    const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)/.exec(rest);
    if (linkMatch) {
      out.push(
        <a
          key={key++}
          href={safeHref(linkMatch[2]!)}
          target="_blank"
          rel="noopener noreferrer"
          className="font-medium text-blue-700 underline decoration-blue-300 underline-offset-4"
        >
          {linkMatch[1]}
        </a>,
      );
      rest = rest.slice(linkMatch[0].length);
      continue;
    }

    const boldMatch = /^\*\*([^*]+)\*\*/.exec(rest);
    if (boldMatch) {
      out.push(
        <strong key={key++} className="font-semibold text-stone-900">
          {boldMatch[1]}
        </strong>,
      );
      rest = rest.slice(boldMatch[0].length);
      continue;
    }

    const italicMatch = /^\*([^*]+)\*/.exec(rest);
    if (italicMatch) {
      out.push(
        <em key={key++} className="italic text-stone-700">
          {italicMatch[1]}
        </em>,
      );
      rest = rest.slice(italicMatch[0].length);
      continue;
    }

    const next = rest.search(/[`*[]/);
    if (next === -1) {
      out.push(rest);
      break;
    }
    if (next === 0) {
      out.push(rest[0]);
      rest = rest.slice(1);
      continue;
    }
    out.push(rest.slice(0, next));
    rest = rest.slice(next);
  }

  return out;
}

function isHorizontalRule(line: string): boolean {
  return /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);
}

function parseBlocks(src: string): Token[] {
  const lines = src.split(/\r?\n/);
  const tokens: Token[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i] ?? "";

    const fence = /^```(\w*)\s*$/.exec(line);
    if (fence) {
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i] ?? "")) {
        codeLines.push(lines[i] ?? "");
        i += 1;
      }
      if (i < lines.length) i += 1;
      tokens.push({ type: "code", lang: fence[1] ?? "", lines: codeLines });
      continue;
    }

    const heading = /^(#{1,6})\s+(.+?)\s*$/.exec(line);
    if (heading) {
      tokens.push({
        type: "heading",
        level: heading[1]!.length as 1 | 2 | 3 | 4 | 5 | 6,
        text: heading[2]!,
      });
      i += 1;
      continue;
    }

    if (isHorizontalRule(line)) {
      tokens.push({ type: "hr" });
      i += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quoteLines: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i] ?? "")) {
        quoteLines.push((lines[i] ?? "").replace(/^\s*>\s?/, ""));
        i += 1;
      }
      tokens.push({ type: "blockquote", lines: quoteLines });
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i] ?? "")) {
        items.push((lines[i] ?? "").replace(/^\s*[-*]\s+/, ""));
        i += 1;
      }
      tokens.push({ type: "ul", items });
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i] ?? "")) {
        items.push((lines[i] ?? "").replace(/^\s*\d+\.\s+/, ""));
        i += 1;
      }
      tokens.push({ type: "ol", items });
      continue;
    }

    if (line.trim() === "") {
      tokens.push({ type: "blank" });
      i += 1;
      continue;
    }

    const paraLines: string[] = [];
    while (
      i < lines.length &&
      (lines[i] ?? "").trim() !== "" &&
      !/^```/.test(lines[i] ?? "") &&
      !/^(#{1,6})\s+/.test(lines[i] ?? "") &&
      !isHorizontalRule(lines[i] ?? "") &&
      !/^\s*>\s?/.test(lines[i] ?? "") &&
      !/^\s*[-*]\s+/.test(lines[i] ?? "") &&
      !/^\s*\d+\.\s+/.test(lines[i] ?? "")
    ) {
      paraLines.push(lines[i] ?? "");
      i += 1;
    }
    tokens.push({ type: "p", lines: paraLines });
  }

  return tokens;
}

function renderTaskItemWithAction(
  line: string,
  key: number,
  taskIndex?: number,
  onToggleTask?: (taskIndex: number) => void,
): ReactNode {
  const taskMatch = /^\[( |x|X)\]\s+(.*)$/.exec(line);
  if (!taskMatch) {
    return <li key={key}>{parseInline(line)}</li>;
  }

  const checked = taskMatch[1]!.toLowerCase() === "x";
  const text = taskMatch[2] ?? "";

  return (
    <li key={key} className="list-none">
      <button
        type="button"
        className="flex w-full items-start gap-2 rounded-xl bg-amber-50/70 px-2.5 py-2 text-left transition-colors hover:bg-amber-100/70"
        onClick={() => {
          if (onToggleTask && typeof taskIndex === "number") {
            onToggleTask(taskIndex);
          }
        }}
      >
        <span
          aria-hidden="true"
          className={[
            "mt-0.5 inline-flex h-4 w-4 flex-none items-center justify-center rounded border text-[10px] font-bold",
            checked
              ? "border-emerald-700 bg-emerald-700 text-white"
              : "border-stone-500/60 bg-white text-transparent",
          ].join(" ")}
        >
          ✓
        </span>
        <span className={checked ? "text-stone-400 line-through" : "text-stone-800"}>
          {parseInline(text)}
        </span>
      </button>
    </li>
  );
}

const HEADING_CLASS: Record<1 | 2 | 3 | 4 | 5 | 6, string> = {
  1: "mt-1 text-[1.05rem] font-semibold leading-snug text-stone-900",
  2: "mt-1 text-[0.98rem] font-semibold leading-snug text-stone-900",
  3: "mt-1 text-[0.92rem] font-semibold leading-snug text-stone-800",
  4: "mt-1 text-[0.88rem] font-semibold leading-snug text-stone-800",
  5: "mt-1 text-[0.84rem] font-semibold leading-snug text-stone-700",
  6: "mt-1 text-[0.8rem] font-semibold uppercase tracking-[0.08em] text-stone-600",
};

export function WhiteboardMarkdown({
  text,
  className,
  onToggleTask,
}: WhiteboardMarkdownProps) {
  const tokens = parseBlocks(text);
  let key = 0;
  let taskIndex = 0;

  return (
    <div className={className}>
      {tokens.map((token) => {
        switch (token.type) {
          case "blank":
            return null;
          case "hr":
            return (
              <div key={key++} className="my-3">
                <hr className="border-0 border-t border-dashed border-stone-300" />
              </div>
            );
          case "code":
            return (
              <pre
                key={key++}
                className="my-3 overflow-x-auto rounded-2xl border border-stone-200 bg-stone-950 px-3 py-2.5 text-xs text-stone-100 shadow-sm"
              >
                <code className="font-mono">{token.lines.join("\n")}</code>
              </pre>
            );
          case "heading": {
            const HeadingTag = `h${token.level}` as const;
            return (
              <HeadingTag key={key++} className={HEADING_CLASS[token.level]}>
                {parseInline(token.text)}
              </HeadingTag>
            );
          }
          case "blockquote":
            return (
              <blockquote
                key={key++}
                className="my-3 rounded-r-2xl rounded-l-md border-l-[3px] border-blue-400 bg-blue-50/80 px-3 py-2 text-[13px] leading-6 text-stone-700"
              >
                {token.lines.map((line, index) => (
                  <Fragment key={index}>
                    {index > 0 ? <br /> : null}
                    {parseInline(line)}
                  </Fragment>
                ))}
              </blockquote>
            );
          case "ul":
            return (
              <ul key={key++} className="my-3 space-y-1.5 pl-0 text-[13px] leading-6">
                {token.items.map((item, index) => {
                  const currentTaskIndex = taskIndex;
                  const isTask = /^\[( |x|X)\]\s+/.test(item);
                  if (isTask) taskIndex += 1;
                  return renderTaskItemWithAction(
                    item,
                    index,
                    isTask ? currentTaskIndex : undefined,
                    onToggleTask,
                  );
                })}
              </ul>
            );
          case "ol":
            return (
              <ol
                key={key++}
                className="my-3 list-decimal space-y-1.5 pl-5 text-[13px] leading-6 marker:font-semibold marker:text-stone-700"
              >
                {token.items.map((item, index) => (
                  <li key={index}>{parseInline(item)}</li>
                ))}
              </ol>
            );
          case "p":
            return (
              <p key={key++} className="my-3 text-[13px] leading-6 text-stone-700">
                {token.lines.map((line, index) => (
                  <Fragment key={index}>
                    {index > 0 ? <br /> : null}
                    {parseInline(line)}
                  </Fragment>
                ))}
              </p>
            );
        }
      })}
    </div>
  );
}

export function toggleTaskAtIndex(content: string, targetTaskIndex: number): string {
  if (targetTaskIndex < 0) return content;

  let currentTaskIndex = 0;
  const lines = content.split(/\r?\n/);
  const updated = lines.map((line) => {
    const match = TASK_LINE_RE.exec(line);
    if (!match) return line;
    if (currentTaskIndex !== targetTaskIndex) {
      currentTaskIndex += 1;
      return line;
    }
    currentTaskIndex += 1;
    const nextFlag = match[2]!.toLowerCase() === "x" ? " " : "x";
    return `${match[1]}${nextFlag}${match[3]}`;
  });

  return updated.join("\n");
}
