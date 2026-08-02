/**
 * Log line formatting strategies for the full-log v0.2 viewer.
 *
 * Pure functions -- no side effects, no external dependencies.
 * Each line is formatted independently; one bad line never breaks others.
 * Traceback blocks are the exception and require multi-line lookahead.
 */

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type LogLevel = "debug" | "info" | "warn" | "error" | "unknown";
export type LineKind = "json" | "plain" | "traceback" | "parse_error";
export type LogFormat = "jsonl" | "plain" | "mixed";

export interface LogLine {
  line_no?: number | null;
  raw: string;
  parsed?: Record<string, unknown> | null;
  parse_error?: string | null;
}

export interface LogLineViewModel {
  /** Unique identity (type-line_no or type-idx). */
  key: string;
  kind: LineKind;
  /** Formatted time excerpt. */
  time?: string;
  level?: LogLevel;
  /** One-line summary. */
  summary: string;
  /** Badges shown at line head (e.g. kind, thread_id). */
  badges: string[];
  /** Original raw text. */
  raw: string;
  /** Pretty-printed JSON for expandable detail. */
  prettyJson?: string;
}

export interface FormatLogLinesOptions {
  sourceType?: string;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_SUMMARY_LEN = 200;
const SESSION_CONVERSATION_TYPE = "session_conversation";

// ---------------------------------------------------------------------------
// Pre-compiled regexes
// ---------------------------------------------------------------------------

/** Python logging: "2024-01-15 10:30:45,123 LEVEL logger message" */
const RE_PYTHON_LOG =
  /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),(\d{3})\s+(\w+)\s+(.+)$/;

/** ISO timestamp prefix: "2024-01-15T10:30:45..." or "2024-01-15 10:30:45..." */
const RE_ISO_PREFIX =
  /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?:[Zz]|[+-]\d{2}:?\d{2})?/;

/** Level keyword anywhere in line (word-boundary). */
const RE_LEVEL_KEYWORD = /\b(ERROR|WARNING|INFO|DEBUG)\b/;

/** Python traceback header. */
const RE_TRACEBACK_HEADER = /^Traceback \(most recent call last\):/;

/**
 * Line that starts with a timestamp, a service log prefix, or is empty/whitespace-only,
 * signalling the end of a traceback block.
 */
const RE_TRACEBACK_BOUNDARY =
  /^(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|(?:INFO|WARNING|ERROR|DEBUG):\s+|\[web\]\s+(?:INFO|WARN|ERROR|DEBUG):?)/;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function truncate(s: string, max: number): string {
  return s.length <= max ? s : s.slice(0, max) + "...";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Pick first non-empty string value from *obj* by trying each *key* in order. */
function pickFirst(
  obj: Record<string, unknown>,
  keys: string[],
): string | undefined {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === "string" && v.length > 0) return v;
  }
  return undefined;
}

function shortId(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length === 0) return undefined;
  return value.length <= 8 ? value : value.slice(0, 8);
}

// ---------------------------------------------------------------------------
// Level normalisation
// ---------------------------------------------------------------------------

const LEVEL_MAP: Record<string, LogLevel> = {
  debug: "debug",
  info: "info",
  warn: "warn",
  warning: "warn",
  error: "error",
};

function normaliseLevel(raw: string | undefined): LogLevel {
  if (!raw) return "unknown";
  return LEVEL_MAP[raw.toLowerCase()] ?? "unknown";
}

function levelFromString(s: string): LogLevel | undefined {
  const m = s.match(RE_LEVEL_KEYWORD);
  if (!m) return undefined;
  const v = m[1].toUpperCase();
  if (v === "ERROR") return "error";
  if (v === "WARNING") return "warn";
  if (v === "INFO") return "info";
  if (v === "DEBUG") return "debug";
  return undefined;
}

// ---------------------------------------------------------------------------
// Time formatting
// ---------------------------------------------------------------------------

/**
 * Convert a numeric millisecond epoch or an ISO/date string to "HH:mm:ss.SSS".
 * Returns *undefined* when the input cannot be interpreted.
 */
function formatTime(value: unknown): string | undefined {
  if (value == null) return undefined;

  // Numeric (millisecond epoch)
  if (typeof value === "number") {
    if (!isFinite(value) || value <= 0) return undefined;
    const d = new Date(value);
    if (isNaN(d.getTime())) return undefined;
    return (
      pad2(d.getHours()) +
      ":" +
      pad2(d.getMinutes()) +
      ":" +
      pad2(d.getSeconds()) +
      "." +
      pad3(d.getMilliseconds())
    );
  }

  // String: try to extract the time portion from ISO / date strings
  if (typeof value === "string") {
    // "HH:mm:ss" or "HH:mm:ss.SSS" already?
    const timeOnly = value.match(/^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)/);
    if (timeOnly) {
      let t = timeOnly[1];
      // Normalise to exactly 3 decimal places
      if (!t.includes(".")) {
        t += ".000";
      } else {
        const dotIdx = t.indexOf(".");
        const frac = t.slice(dotIdx + 1);
        if (frac.length < 3) {
          t += "0".repeat(3 - frac.length);
        } else if (frac.length > 3) {
          t = t.slice(0, dotIdx + 4);
        }
      }
      return t;
    }

    // ISO / date string -- parse and format
    const d = new Date(value);
    if (!isNaN(d.getTime())) {
      return (
        pad2(d.getHours()) +
        ":" +
        pad2(d.getMinutes()) +
        ":" +
        pad2(d.getSeconds()) +
        "." +
        pad3(d.getMilliseconds())
      );
    }
  }

  return undefined;
}

function pad2(n: number): string {
  return n < 10 ? "0" + n : "" + n;
}

function pad3(n: number): string {
  return n < 10 ? "00" + n : n < 100 ? "0" + n : "" + n;
}

function formatEpochSecondsOrMs(value: unknown): string | undefined {
  if (typeof value !== "number") return formatTime(value);
  if (!isFinite(value) || value <= 0) return undefined;
  const ms = value < 10_000_000_000 ? value * 1000 : value;
  return formatTime(ms);
}

// ---------------------------------------------------------------------------
// Badge extraction
// ---------------------------------------------------------------------------

const BADGE_KEYS = ["kind", "thread_id", "run_id", "channel", "dir"] as const;

function extractBadges(parsed: Record<string, unknown>): string[] {
  const badges: string[] = [];
  for (const k of BADGE_KEYS) {
    const v = parsed[k];
    if (typeof v === "string" && v.length > 0 && v.length <= 40) {
      badges.push(v);
    }
  }
  return badges;
}

// ---------------------------------------------------------------------------
// Single-line formatters
// ---------------------------------------------------------------------------

/**
 * Format a line whose `parsed` field is present (JSONL / JSON).
 */
function formatJsonLine(line: LogLine, idx: number): LogLineViewModel {
  const parsed = line.parsed ?? {};
  const key = "json-" + (line.line_no ?? idx);

  // Time: ts > timestamp > time
  const time =
    formatTime(parsed["ts"]) ??
    formatTime(parsed["timestamp"]) ??
    formatTime(parsed["time"]);

  // Level: level > severity
  const level = normaliseLevel(
    typeof parsed["level"] === "string"
      ? parsed["level"]
      : typeof parsed["severity"] === "string"
        ? parsed["severity"]
        : undefined,
  );

  // Summary: message > msg > error > event > kind
  const rawSummary =
    pickFirst(parsed, ["message", "msg", "error", "event", "kind"]) ?? "";

  const badges = extractBadges(parsed);

  let prettyJson: string | undefined;
  try {
    prettyJson = JSON.stringify(parsed, null, 2);
  } catch {
    prettyJson = undefined;
  }

  return {
    key,
    kind: "json",
    time,
    level,
    summary: truncate(rawSummary, MAX_SUMMARY_LEN),
    badges,
    raw: line.raw,
    prettyJson,
  };
}

function prettyPrintJson(parsed: Record<string, unknown>): string | undefined {
  try {
    return JSON.stringify(parsed, null, 2);
  } catch {
    return undefined;
  }
}

function sessionMessageRecord(
  parsed: Record<string, unknown>,
): Record<string, unknown> {
  const message = parsed["message"];
  return isRecord(message) ? message : {};
}

function sessionToolCallNames(message: Record<string, unknown>): string {
  const toolCalls = message["tool_calls"];
  if (!Array.isArray(toolCalls) || toolCalls.length === 0) return "";
  const names = toolCalls
    .map((call) => {
      if (!isRecord(call)) return undefined;
      const toolName = call["tool_name"];
      return typeof toolName === "string" && toolName.length > 0
        ? toolName
        : undefined;
    })
    .filter((name): name is string => name !== undefined);
  if (names.length === 0) return "tool calls";
  return "tool calls: " + names.join(", ");
}

function sessionSummaryBody(message: Record<string, unknown>): string {
  const content = message["content"];
  if (typeof content === "string" && content.trim().length > 0) {
    return content.trim();
  }
  return sessionToolCallNames(message);
}

function sessionRole(message: Record<string, unknown>): string {
  const role = message["role"];
  return typeof role === "string" && role.length > 0 ? role : "message";
}

function formatSessionConversationLine(
  line: LogLine,
  idx: number,
): LogLineViewModel {
  if (line.parse_error) {
    return formatParseErrorLine(line, idx);
  }
  if (!line.parsed) {
    return formatPlainLine(line, idx);
  }

  const parsed = line.parsed;
  const message = sessionMessageRecord(parsed);
  const role = sessionRole(message);
  const roleLabel = role.toUpperCase();
  const messageId = shortId(parsed["message_id"]) ?? `line${line.line_no ?? idx}`;
  const body = sessionSummaryBody(message);
  const summaryPrefix = `${messageId} · ${roleLabel}`;
  const summary = body
    ? `${summaryPrefix} · ${truncate(body, MAX_SUMMARY_LEN)}`
    : summaryPrefix;

  const badges = [roleLabel];
  const parentId = shortId(parsed["parent_message_id"]);
  if (parentId) {
    badges.push(`parent:${parentId}`);
  }

  return {
    key: "session-" + (line.line_no ?? idx),
    kind: "json",
    time:
      formatEpochSecondsOrMs(parsed["created_at"]) ??
      formatTime(parsed["ts"]) ??
      formatTime(parsed["timestamp"]) ??
      formatTime(parsed["time"]),
    level: "info",
    summary,
    badges,
    raw: line.raw,
    prettyJson: prettyPrintJson(parsed),
  };
}

/**
 * Format a line that has a `parse_error` -- the JSON parse failed but we
 * still have the original raw text.
 */
function formatParseErrorLine(line: LogLine, idx: number): LogLineViewModel {
  const key = "parse-error-" + (line.line_no ?? idx);
  return {
    key,
    kind: "parse_error",
    level: "unknown",
    summary: truncate(line.raw, MAX_SUMMARY_LEN),
    badges: ["parse-error"],
    raw: line.raw,
  };
}

/**
 * Format a plain-text line (no `parsed`, no `parse_error`).
 * Detects Python logging, ISO timestamps, and level keywords.
 */
function formatPlainLine(line: LogLine, idx: number): LogLineViewModel {
  const key = "plain-" + (line.line_no ?? idx);
  const raw = line.raw;

  // 1) Python logging: "YYYY-MM-DD HH:mm:ss,SSS LEVEL logger message"
  const pyMatch = raw.match(RE_PYTHON_LOG);
  if (pyMatch) {
    const time = pyMatch[1].split(" ")[1]; // "HH:mm:ss"
    const levelStr = pyMatch[3];
    const rest = pyMatch[4];
    return {
      key,
      kind: "plain",
      time,
      level: normaliseLevel(levelStr),
      summary: truncate(rest, MAX_SUMMARY_LEN),
      badges: [],
      raw,
    };
  }

  // 2) ISO timestamp prefix
  const isoMatch = raw.match(RE_ISO_PREFIX);
  if (isoMatch) {
    const time = formatTime(isoMatch[1]);
    const after = raw.slice(isoMatch[0].length).trimStart();
    const level = levelFromString(after) ?? levelFromString(raw);
    return {
      key,
      kind: "plain",
      time,
      level,
      summary: truncate(after || raw, MAX_SUMMARY_LEN),
      badges: [],
      raw,
    };
  }

  // 3) Level keyword anywhere
  const detectedLevel = levelFromString(raw);
  if (detectedLevel) {
    return {
      key,
      kind: "plain",
      level: detectedLevel,
      summary: truncate(raw, MAX_SUMMARY_LEN),
      badges: [],
      raw,
    };
  }

  // 4) Unrecognised
  return {
    key,
    kind: "plain",
    level: "unknown",
    summary: truncate(raw, MAX_SUMMARY_LEN),
    badges: [],
    raw,
  };
}

// ---------------------------------------------------------------------------
// Traceback merging (multi-line lookahead)
// ---------------------------------------------------------------------------

/**
 * A traceback block starts with `Traceback (most recent call last):`
 * and continues until a timestamp line, an empty line, or EOF.
 *
 * All lines in the block are merged into a single `LogLineViewModel`
 * with `kind="traceback"`.
 */
function formatTracebackBlock(
  lines: LogLine[],
  startIdx: number,
): { model: LogLineViewModel; consumedCount: number } {
  const first = lines[startIdx];
  const collectedRaw: string[] = [first.raw];
  let endIdx = startIdx + 1;

  while (endIdx < lines.length) {
    const candidate = lines[endIdx].raw.trim();
    // Stop at empty line, timestamp-prefixed line, or a new service log line.
    if (candidate === "" || RE_TRACEBACK_BOUNDARY.test(candidate)) break;
    collectedRaw.push(lines[endIdx].raw);
    endIdx++;
  }

  const mergedRaw = collectedRaw.join("\n");
  const lastLine = collectedRaw[collectedRaw.length - 1].trim();

  return {
    model: {
      key: "traceback-" + (first.line_no ?? startIdx),
      kind: "traceback",
      level: "error",
      summary: truncate(lastLine, MAX_SUMMARY_LEN),
      badges: ["traceback"],
      raw: mergedRaw,
    },
    consumedCount: endIdx - startIdx,
  };
}

// ---------------------------------------------------------------------------
// Per-format pipelines
// ---------------------------------------------------------------------------

/**
 * JSONL pipeline: every line with `parsed` becomes json; parse_error lines
 * get their own kind; remaining lines fall through to plain.
 */
function formatAsJsonl(lines: LogLine[]): LogLineViewModel[] {
  const results: LogLineViewModel[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.parse_error) {
      results.push(formatParseErrorLine(line, i));
    } else if (line.parsed) {
      results.push(formatJsonLine(line, i));
    } else {
      results.push(formatPlainLine(line, i));
    }
  }
  return results;
}

function formatAsSessionConversation(lines: LogLine[]): LogLineViewModel[] {
  const results: LogLineViewModel[] = [];
  for (let i = 0; i < lines.length; i++) {
    results.push(formatSessionConversationLine(lines[i], i));
  }
  return results;
}

/**
 * Plain pipeline: plain-text lines with traceback block merging.
 */
function formatAsPlain(lines: LogLine[]): LogLineViewModel[] {
  const results: LogLineViewModel[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    // Check for traceback start -- only when there is no parse_error and
    // the raw line matches the traceback header pattern.
    if (!line.parse_error && RE_TRACEBACK_HEADER.test(line.raw.trim())) {
      const { model, consumedCount } = formatTracebackBlock(lines, i);
      results.push(model);
      i += consumedCount;
    } else if (line.parse_error) {
      results.push(formatParseErrorLine(line, i));
      i++;
    } else if (line.parsed) {
      // Even in "plain" mode, if the backend already parsed JSON we can
      // still present it as a json line for richer formatting.
      results.push(formatJsonLine(line, i));
      i++;
    } else {
      results.push(formatPlainLine(line, i));
      i++;
    }
  }
  return results;
}

/**
 * Mixed pipeline: try JSON first (use parsed if available), otherwise plain
 * with traceback merging.
 */
function formatAsMixed(lines: LogLine[]): LogLineViewModel[] {
  // Mixed is identical to plain because the backend already populates
  // `parsed` for JSON lines.  The traceback merging benefits all modes.
  return formatAsPlain(lines);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Format an array of `LogLine` into display-ready `LogLineViewModel[]`.
 *
 * @param lines   Raw log lines (from backend `LogReadResponseDTO.lines`).
 * @param format  The source format hint: "jsonl", "plain", or "mixed".
 *                - "jsonl"  -- per-line JSON formatting, no traceback merging.
 *                - "plain"  -- plain text formatting with traceback block merging.
 *                - "mixed"  -- try JSON first, fall back to plain + traceback.
 */
export function formatLogLines(
  lines: LogLine[],
  format: LogFormat,
  options: FormatLogLinesOptions = {},
): LogLineViewModel[] {
  if (!lines || lines.length === 0) return [];
  if (options.sourceType === SESSION_CONVERSATION_TYPE) {
    return formatAsSessionConversation(lines);
  }

  switch (format) {
    case "jsonl":
      return formatAsJsonl(lines);
    case "plain":
      return formatAsPlain(lines);
    case "mixed":
      return formatAsMixed(lines);
    default:
      return formatAsPlain(lines);
  }
}
