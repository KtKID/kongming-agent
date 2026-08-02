import { ArrowUp, ArrowDown, Brain, Clock, Cpu, DatabaseZap, Sparkles, Gauge, Timer } from "lucide-react";
import { useChatStore } from "@/stores/chat";
import { useThreadsStore } from "@/stores/threads";
import type { ReasoningEffort } from "@/components/Composer";
import type {
  ClaudeUsage,
  CodexUsage,
  GenericChatAnthropicUsage,
  GenericChatOpenAIUsage,
  ThreadUsage,
} from "@/protocol";

function fmt(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function Stat({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ElementType;
  label: string;
  value: string;
}) {
  return (
    <span className="inline-flex items-center gap-1" title={label}>
      <Icon className="h-3 w-3" />
      <span>{value}</span>
    </span>
  );
}

/**
 * 上下文占用进度条：`当前 X / 上限 Y (Z%)` + ASCII 进度条。
 *
 * 未命中模型上限时仅显示当前占用数字，不显示百分比 / 进度条。
 */
function ContextUsageBar({
  current,
  limit,
}: {
  current: number | null;
  limit: number;
}) {
  if (current == null) {
    return <Stat icon={Gauge} label="当前上下文占用未知" value="上下文 —" />;
  }
  if (!limit || limit <= 0) {
    return <Stat icon={Gauge} label="当前上下文占用（模型上限未知）" value={`上下文 ${fmt(current)}`} />;
  }
  const pct = +((100 * current) / limit).toFixed(1);
  const filled = Math.min(10, Math.round((pct / 100) * 10));
  const bar = "▓".repeat(filled) + "░".repeat(10 - filled);
  return (
    <span
      className="inline-flex items-center gap-1"
      title={`当前上下文 ${fmt(current)} / 上限 ${fmt(limit)} (${pct.toFixed(1)}%)`}
    >
      <Gauge className="h-3 w-3" />
      <span>
        上下文 {fmt(current)} / {fmt(limit)} ({pct.toFixed(0)}%) {bar}
      </span>
    </span>
  );
}

/**
 * Claude 系（claude_code / generic_chat-anthropic）底栏字段（v2）。
 *
 * 显示：输入（纯新增）/ 缓存命中 / 缓存写入 / [1h TTL 写入]? / [5m TTL 写入]? / 输出 / 上下文占用进度条
 *
 * usage-token-v2-ui-fields: 1h / 5m TTL 细分（``cache_creation.ephemeral_*``）
 * 只在对应值 > 0 时渲染，避免空数据噪声。
 */
function StatusLineClaude({
  usage,
}: {
  usage: ClaudeUsage | GenericChatAnthropicUsage;
}) {
  const c1h = usage.cache_creation.ephemeral_1h_input_tokens;
  const c5m = usage.cache_creation.ephemeral_5m_input_tokens;
  return (
    <>
      <Stat icon={ArrowUp} label="本轮纯新增输入（不含 cache）" value={`输入 ${fmt(usage.input_tokens)}`} />
      <Stat icon={DatabaseZap} label="本轮命中 prompt cache" value={`缓存命中 ${fmt(usage.cache_read_input_tokens)}`} />
      <Stat icon={Sparkles} label="本轮新写入 cache（1h+5m 合计）" value={`缓存写入 ${fmt(usage.cache_creation_input_tokens)}`} />
      {c1h != null && c1h > 0 && (
        <Stat icon={Clock} label="本轮新写入 cache · 1h TTL" value={`1h ${fmt(c1h)}`} />
      )}
      {c5m != null && c5m > 0 && (
        <Stat icon={Timer} label="本轮新写入 cache · 5m TTL" value={`5m ${fmt(c5m)}`} />
      )}
      <Stat icon={ArrowDown} label="本轮输出（含 thinking）" value={`输出 ${fmt(usage.output_tokens)}`} />
      <ContextUsageBar current={usage.context_usage} limit={usage.context_window} />
    </>
  );
}

/**
 * Codex 系底栏字段（v2）：显示 total + last。
 *
 * codex 自带累加 + context_window；显示 total 累计 + 上下文进度条。
 */
function StatusLineCodex({ usage }: { usage: CodexUsage }) {
  return (
    <>
      <Stat icon={ArrowUp} label="累计输入（含 cache 子集）" value={`输入 ${fmt(usage.total.input_tokens)}`} />
      <Stat icon={DatabaseZap} label="累计命中 cache" value={`缓存命中 ${fmt(usage.total.cached_input_tokens)}`} />
      <Stat icon={ArrowDown} label="累计输出（含 reasoning 子集）" value={`输出 ${fmt(usage.total.output_tokens)}`} />
      <Stat icon={Brain} label="累计 reasoning（output 子集）" value={`推理 ${fmt(usage.total.reasoning_output_tokens)}`} />
      <ContextUsageBar current={usage.last.input_tokens} limit={usage.model_context_window} />
    </>
  );
}

/**
 * generic_chat-openai 通道（底层非 codex，没 codex 自带的累加/rate_limits）。
 *
 * 显示：last 4 字段 + 上下文进度条。
 */
function StatusLineGenericChatOpenAI({ usage }: { usage: GenericChatOpenAIUsage }) {
  return (
    <>
      <Stat icon={ArrowUp} label="本轮输入（含 cache 子集）" value={`输入 ${fmt(usage.last.input_tokens)}`} />
      <Stat icon={DatabaseZap} label="本轮命中 cache" value={`缓存命中 ${fmt(usage.last.cached_input_tokens)}`} />
      <Stat icon={ArrowDown} label="本轮输出（含 reasoning 子集）" value={`输出 ${fmt(usage.last.output_tokens)}`} />
      <Stat icon={Brain} label="本轮 reasoning（output 子集）" value={`推理 ${fmt(usage.last.reasoning_output_tokens)}`} />
      <ContextUsageBar current={usage.last.input_tokens} limit={usage.context_window} />
    </>
  );
}

/** discriminator：CodexUsage 有 ``total`` + ``rate_limits``；GenericChatOpenAIUsage 没有。 */
function isCodex(usage: ThreadUsage): usage is CodexUsage {
  return usage.provider === "openai" && "total" in usage;
}

/**
 * 提取模型名（usage-token-v2-ui-fields）。
 *
 * 优先级：
 * 1. ``ClaudeUsage.model``（claude_code / generic_chat-anthropic 直拿 SDK 真源）
 * 2. ``GenericChatOpenAIUsage.model``
 * 3. Codex 无 ``model`` 字段 → fallback 到 ``threadModel``（thread preset_id）
 * 4. usage 缺失或字段为空 → ``threadModel``
 */
function pickModelName(usage: ThreadUsage | null, threadModel: string): string {
  if (!usage) return threadModel;
  if (usage.provider === "claude") return usage.model || threadModel;
  if (!isCodex(usage)) return usage.model || threadModel;
  return threadModel;
}

function reasoningEffortLabel(effort: ReasoningEffort | null | undefined): string | null {
  if (!effort || effort === "none") return null;
  if (effort === "low") return "低";
  if (effort === "medium") return "中";
  if (effort === "high") return "高";
  return "最高";
}

interface StatusLineProps {
  threadId: string | undefined;
  reasoningEffort?: ReasoningEffort | null;
}

/**
 * 聊天底部状态栏（usage-token-v2-bigbang 重构）。
 *
 * 按 ``usage.provider`` discriminator 分支：
 * - `"claude"` → `StatusLineClaude`（claude_code / generic_chat-anthropic 共享）
 * - `"openai"` + 有 `total` → `StatusLineCodex`
 * - `"openai"` + 无 `total` → `StatusLineGenericChatOpenAI`
 *
 * thread 首轮前 usage == null 时只显示「等待对话」占位。
 */
export function StatusLine({ threadId, reasoningEffort }: StatusLineProps) {
  const usage = useChatStore((s) =>
    threadId ? (s.usageByThread[threadId] ?? null) : null,
  );
  const threadModel = useThreadsStore((s) => {
    if (!threadId) return "";
    const t = s.threads.find((x) => x.id === threadId);
    if (!t) return "";
    return t.preset_id ?? "";
  });

  const modelName = pickModelName(usage, threadModel);
  const effortLabel = reasoningEffortLabel(reasoningEffort);

  return (
    <div className="mx-auto flex max-w-3xl items-center justify-between px-0 py-px text-[8px] tabular-nums text-muted-foreground">
      <span className="inline-flex items-center gap-3">
        {effortLabel && (
          <span className="inline-flex items-center gap-1 text-primary">
            <Brain className="h-3 w-3" />
            深度思考 {effortLabel}
          </span>
        )}
        {modelName && (
          <span className="inline-flex items-center gap-1 text-muted-foreground/70" title="当前模型">
            <Cpu className="h-3 w-3" />
            <span>{modelName}</span>
          </span>
        )}
        {!effortLabel && !modelName && <span />}
      </span>
      <span className="inline-flex items-center gap-3">
        {usage == null && (
          <span className="text-muted-foreground/60">等待对话</span>
        )}
        {usage != null && usage.provider === "claude" && (
          <StatusLineClaude usage={usage} />
        )}
        {usage != null && usage.provider === "openai" && isCodex(usage) && (
          <StatusLineCodex usage={usage} />
        )}
        {usage != null && usage.provider === "openai" && !isCodex(usage) && (
          <StatusLineGenericChatOpenAI usage={usage} />
        )}
      </span>
    </div>
  );
}
