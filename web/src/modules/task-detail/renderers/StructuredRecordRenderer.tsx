import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { Markdown } from "@/lib/markdown";
import { cn } from "@/lib/utils";

interface FieldView {
  key: string;
  value: unknown;
}

const MARKDOWN_KEYS = new Set(["content", "text"]);
const META_KEYS = new Set([
  "role",
  "type",
  "event",
  "action",
  "timestamp",
  "created_at",
  "ts",
  "message_id",
  "session_id",
]);

export function StructuredRecordRenderer({
  record,
  index,
}: {
  record: unknown;
  index: number;
}) {
  if (!isRecord(record)) {
    return (
      <RecordShell index={index} title="value">
        <JsonBlock value={record} />
      </RecordShell>
    );
  }

  if (record.__parse_error__ === true) {
    return (
      <RecordShell
        index={index}
        title={`parse error line ${String(record.line ?? index + 1)}`}
        tone="error"
      >
        <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="min-w-0">
            <div className="font-medium">{String(record.error ?? "JSON parse failed")}</div>
            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs">
              {String(record.raw ?? "")}
            </pre>
          </div>
        </div>
      </RecordShell>
    );
  }

  const classified = classifyRecord(record);
  return (
    <RecordShell index={index} title={classified.title}>
      {classified.metadata.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {classified.metadata.map((field) => (
            <span
              key={field.key}
              className="rounded-md border border-border/70 bg-muted/60 px-2 py-1 text-[11px] text-muted-foreground"
            >
              <span className="font-medium text-foreground">{field.key}</span>
              {" "}
              {primitiveText(field.value)}
            </span>
          ))}
        </div>
      ) : null}
      {classified.markdown.length > 0 ? (
        <div className="space-y-3">
          {classified.markdown.map((field) => (
            <section key={field.key} className="min-w-0 rounded-lg border border-border/70 bg-card/70 p-3">
              <div className="mb-2 text-[11px] font-medium uppercase text-muted-foreground">
                {field.key}
              </div>
              <Markdown
                text={withMarkdownBreaks(String(field.value ?? ""))}
                className="text-sm leading-relaxed"
              />
            </section>
          ))}
        </div>
      ) : null}
      {classified.json.length > 0 ? (
        <div className="space-y-2">
          {classified.json.map((field) => (
            <details
              key={field.key}
              className="rounded-lg border border-border/70 bg-background/70"
            >
              <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-foreground">
                {field.key}
              </summary>
              <JsonBlock value={field.value} className="border-t border-border/70" />
            </details>
          ))}
        </div>
      ) : null}
    </RecordShell>
  );
}

function RecordShell({
  index,
  title,
  tone = "default",
  children,
}: {
  index: number;
  title: string;
  tone?: "default" | "error";
  children: ReactNode;
}) {
  return (
    <article
      className={cn(
        "min-w-0 rounded-xl border bg-card/55 p-3",
        tone === "error" ? "border-destructive/30" : "border-border/70",
      )}
      data-testid="structured-record"
    >
      <div className="mb-3 flex min-w-0 items-center justify-between gap-3">
        <div className="min-w-0 truncate text-sm font-semibold text-foreground">
          {title}
        </div>
        <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
          #{index + 1}
        </span>
      </div>
      <div className="space-y-3">{children}</div>
    </article>
  );
}

export function JsonBlock({
  value,
  className,
}: {
  value: unknown;
  className?: string;
}) {
  return (
    <pre
      className={cn(
        "max-h-[36rem] overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed text-muted-foreground",
        className,
      )}
    >
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function classifyRecord(record: Record<string, unknown>) {
  const metadata: FieldView[] = [];
  const markdown: FieldView[] = [];
  const json: FieldView[] = [];

  for (const [key, value] of Object.entries(record)) {
    if (key === "message" && isRecord(value)) {
      const role = value.role;
      const content = value.content;
      if (role != null) metadata.push({ key: "message.role", value: role });
      if (typeof content === "string") {
        markdown.push({ key: "message.content", value: content });
        const rest = { ...value };
        delete rest.role;
        delete rest.content;
        if (Object.keys(rest).length > 0) json.push({ key: "message", value: rest });
      } else {
        json.push({ key, value });
      }
      continue;
    }
    if (key === "message" && typeof value === "string") {
      markdown.push({ key, value });
      continue;
    }
    if (MARKDOWN_KEYS.has(key) && typeof value === "string") {
      markdown.push({ key, value });
      continue;
    }
    if (META_KEYS.has(key) && isPrimitive(value)) {
      metadata.push({ key, value });
      continue;
    }
    if (isPrimitive(value)) {
      metadata.push({ key, value });
      continue;
    }
    json.push({ key, value });
  }

  return {
    title: titleFor(metadata, record),
    metadata,
    markdown,
    json,
  };
}

function titleFor(metadata: FieldView[], record: Record<string, unknown>): string {
  const action =
    metadata.find((field) => ["action", "event", "type", "role", "message.role"].includes(field.key))
      ?.value ?? record.message_id ?? "record";
  return String(action);
}

function withMarkdownBreaks(text: string): string {
  return text.replace(/\n(?!\n)/g, "  \n");
}

function primitiveText(value: unknown): string {
  if (value == null) return "-";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function isPrimitive(value: unknown): boolean {
  return value == null || ["string", "number", "boolean"].includes(typeof value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
