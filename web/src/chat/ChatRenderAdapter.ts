/**
 * chat-receive-side-unify #2 · ChatRenderAdapter（渲染适配层）
 *
 * 把 `ChatTimelineState`（数据真源，#1）投影成视图能消费的渲染清单。双层设计：
 *
 * 1) 核心层 `toViewModel(state) → ChatViewModel`（spec 04 真源）：
 *    纯函数，无副作用，便于 #3 在组件侧 useMemo。未来换 Streamdown 等渲染层
 *    只动这一层，适配层 / 视图不动。
 *
 * 2) 适配层 `toGenericRenderItems` / `toClaudeRenderItems` / `toCodexRenderItems`：
 *    把通用 ViewModel 翻回各频道**现有** RenderItem 类型，保 UI 零回归。
 *    - GenericChatItem = stores/chat.ts ChatItem（7 kind），是真身。
 *    - ClaudeRenderItem = GenericChatItem | ClaudeMetaItem（status/complete meta
 *      不在时间线 state 里，由 #6 视图侧追加，本层只产出 GenericChatItem 部分）。
 *    - CodexRenderItem = GenericChatItem | CodexMetaItem（同上，#7）。
 *
 * ## turn / runId
 *
 * 时间线记录直接保存结构化 `runId` / `turn`。`turnId` 只作为内部归并 key，
 * 适配层不再从字符串反解运行坐标。
 */
import type {
  ChatTimelineState,
  ChatViewModel,
  ChatViewItem,
  ChatMessageRecord,
  ChatToolRecord,
  ChatPendingTool,
  ConversationReferenceDTO,
  UserInputAttachment,
} from "@/chat/types";
import type { ChatItem as GenericChatItem } from "@/stores/chat";

// ClaudeRenderItem / CodexRenderItem 的 union 真源在视图组件里
// （components/ClaudeCodeView.tsx / CodexView.tsx）。本层产出的是其中的
// GenericChatItem 分支；meta 项（status/complete）由 #6/#7 视图侧追加。
type ClaudeMetaItem =
  | { kind: "status"; content?: unknown; id: string }
  | { kind: "complete"; aborted?: boolean; id: string };
type CodexMetaItem =
  | { kind: "status"; content?: unknown; id: string }
  | { kind: "complete"; aborted?: boolean; id: string };
export type ClaudeRenderItem = GenericChatItem | ClaudeMetaItem;
export type CodexRenderItem = GenericChatItem | CodexMetaItem;

// ---------------------------------------------------------------------------
// 核心层：state → ChatViewModel（纯函数）
// ---------------------------------------------------------------------------

function viewTurn(turn: number | null): number {
  return turn ?? 0;
}

/** 取消息正文文本（合并所有 text part）。 */
function textOf(record: ChatMessageRecord): string {
  return record.parts
    .filter((p): p is { type: "text"; text: string } => p.type === "text")
    .map((p) => p.text)
    .join("");
}

/** 取消息附件（从 attachment part 还原）。 */
function attachmentsOf(record: ChatMessageRecord): UserInputAttachment[] | undefined {
  const atts = record.parts
    .filter((p): p is { type: "attachment"; attachment: UserInputAttachment } => p.type === "attachment")
    .map((p) => p.attachment);
  return atts.length > 0 ? atts : undefined;
}

function referencesOf(record: ChatMessageRecord): ConversationReferenceDTO[] | undefined {
  return record.references && record.references.length > 0
    ? record.references
    : undefined;
}

/** tool 富记录（已 resolve）投影成 tool 视图项。 */
function toolToViewItem(tool: ChatToolRecord, threadId: string): Extract<ChatViewItem, { kind: "tool" }> {
  const ok: boolean | null =
    tool.status === "completed" ? true : tool.status === "failed" ? false : null;
  return {
    kind: "tool",
    id: tool.id,
    threadId,
    turn: viewTurn(tool.turn),
    runId: tool.runId,
    toolName: tool.toolName,
    callId: tool.id,
    arguments: tool.arguments ?? {},
    ok,
    errorMessage: tool.errorMessage,
    result: tool.outputText.length > 0 ? tool.outputText : undefined,
    resultData: tool.resultData ?? null,
    partialInput: tool.partialInput,
    pending: tool.pending,
    timestampMs: tool.startedAt,
  };
}

/** pending tool 占位（claude 流式参数构建中，未进 toolsById）投影成 tool 视图项。 */
function pendingToolToViewItem(
  pending: ChatPendingTool,
  threadId: string,
): Extract<ChatViewItem, { kind: "tool" }> {
  return {
    kind: "tool",
    id: pending.id,
    threadId,
    turn: viewTurn(pending.turn),
    runId: pending.runId,
    toolName: "",
    callId: pending.id,
    arguments: {},
    ok: null,
    result: undefined,
    resultData: null,
    partialInput: pending.partialInput,
    pending: true,
    timestampMs: pending.startedAt,
  };
}

/**
 * 核心层投影：把 ChatTimelineState 投成 ChatViewModel。
 *
 * 顺序严格按 `state.orderedMessageIds`。role="tool" 的轻 record 回查
 * `toolsById`（已 resolve）/ `pendingTools`（占位中）取富字段。
 */
export function toViewModel(state: ChatTimelineState): ChatViewModel {
  const items: ChatViewItem[] = [];

  for (const id of state.orderedMessageIds) {
    const record = state.messagesById[id];
    if (!record) continue;

    switch (record.role) {
      case "user":
      case "assistant": {
        const content = textOf(record);
        items.push({
          kind: "message",
          id: record.id,
          role: record.role,
          threadId: record.threadId,
          turn: viewTurn(record.turn),
          runId: record.runId,
          content,
          reasoning: record.reasoning,
          usage: record.usage,
          forkHistoryIndex: record.forkHistoryIndex,
          attachments: attachmentsOf(record),
          references: record.role === "user" ? referencesOf(record) : undefined,
          deliveryStatus: record.role === "user" ? record.deliveryStatus : undefined,
          streaming:
            record.role === "assistant" &&
            content.length > 0 &&
            record.status === "streaming",
          timestampMs: record.createdAt,
        });
        break;
      }
      case "tool": {
        // 富数据在 toolsById（resolve 后）或 pendingTools（占位中）。
        const tool = state.toolsById[record.id];
        if (tool) {
          items.push(toolToViewItem(tool, state.threadId));
        } else {
          const pending = state.pendingTools[record.id];
          if (pending) items.push(pendingToolToViewItem(pending, state.threadId));
        }
        break;
      }
      case "system": {
        const notice = record.notice;
        if (!notice) break;
        // usage 留 store 供 StatusLine 消费，不渲染为系统通知卡片。
        if (notice.source === "usage") break;
        items.push({
          kind: "notice",
          id: record.id,
          threadId: record.threadId,
          runId: record.runId,
          noticeKey: notice.noticeKey,
          source: notice.source,
          title: notice.title,
          message: notice.message,
          details: notice.details,
          detailsData: notice.detailsData,
          status: notice.status,
          icon: notice.icon,
          timestampMs: record.createdAt,
        });
        break;
      }
      case "error": {
        const err = record.error;
        if (!err) break;
        items.push({
          kind: "error",
          id: record.id,
          threadId: record.threadId,
          message: err.message,
          errorCode: err.errorCode,
          timestampMs: record.createdAt,
        });
        break;
      }
      default:
        break;
    }
  }

  // pendingTools 不一定有对应的 orderedMessageIds 槽位（占位期 store 不写 message
  // record），补齐尚未出现在 items 里的 pending 占位，保证 claude 流式参数构建态可见。
  const seen = new Set(items.filter((i) => i.kind === "tool").map((i) => i.id));
  for (const pending of Object.values(state.pendingTools)) {
    if (!seen.has(pending.id)) {
      items.push(pendingToolToViewItem(pending, state.threadId));
    }
  }

  return {
    items,
    isStreaming: state.activeStreamingTurnId != null,
    historyLoaded: state.historyLoaded,
  };
}

// ---------------------------------------------------------------------------
// 适配层：ChatViewModel → 各频道现有 RenderItem
// ---------------------------------------------------------------------------

/** 单个 ChatViewItem → GenericChatItem（与 stores/chat.ts ChatItem 逐字段等价）。 */
function viewItemToGeneric(item: ChatViewItem): GenericChatItem {
  switch (item.kind) {
    case "message":
      if (item.role === "user") {
        return {
          id: item.id,
          kind: "user",
          threadId: item.threadId,
          content: item.content,
          timestampMs: item.timestampMs,
          attachments: item.attachments,
          references: item.references,
          deliveryStatus: item.deliveryStatus,
        };
      }
      return {
        id: item.id,
        kind: "assistant",
        threadId: item.threadId,
        turn: item.turn,
        runId: item.runId,
        content: item.content,
        reasoning: item.reasoning ?? "",
        usage: item.usage,
        forkHistoryIndex: item.forkHistoryIndex,
        timestampMs: item.timestampMs,
        streaming: item.streaming,
      };
    case "tool":
      return {
        id: item.id,
        kind: "tool",
        threadId: item.threadId,
        turn: item.turn,
        runId: item.runId,
        toolName: item.toolName,
        callId: item.callId,
        arguments: item.arguments,
        ok: item.ok,
        errorMessage: item.errorMessage,
        result: item.result,
        resultData: item.resultData,
        partialInput: item.partialInput,
        pending: item.pending,
        timestampMs: item.timestampMs,
      };
    case "notice":
      // status / icon 在 #1 store 落库前已归一到 SystemChatStatus / SystemChatIcon
      // 取值（见 ChatTimelineStore.upsertNotice 复用 normalizeSystemNotice* 语义），
      // 这里直接搬运，断言用 SystemChatStatus / SystemChatIcon。
      return {
        id: item.id,
        kind: "system",
        threadId: item.threadId,
        runId: item.runId,
        noticeKey: item.noticeKey,
        source: item.source,
        title: item.title,
        message: item.message,
        details: item.details,
        detailsData: item.detailsData,
        status: item.status as GenericSystemStatus,
        icon: item.icon as GenericSystemIcon,
        timestampMs: item.timestampMs,
      };
    case "error":
      return {
        id: item.id,
        kind: "error",
        threadId: item.threadId,
        message: item.message,
        errorCode: item.errorCode,
        timestampMs: item.timestampMs,
      };
    default: {
      const _exhaustive: never = item;
      return _exhaustive;
    }
  }
}

// system kind 的 status/icon 字面量类型（与 stores/chat.ts 等价；ViewModel 用宽松
// string 承载，落 GenericChatItem 时收窄）。
type GenericSystemStatus = Extract<GenericChatItem, { kind: "system" }>["status"];
type GenericSystemIcon = Extract<GenericChatItem, { kind: "system" }>["icon"];

/** 通用频道渲染清单：ChatViewModel → GenericChatItem[]。 */
export function toGenericRenderItems(view: ChatViewModel): GenericChatItem[] {
  return view.items.map(viewItemToGeneric);
}

/**
 * Claude 频道渲染清单：ChatViewModel → ClaudeRenderItem[]。
 *
 * 绝大多数 item 复用 generic 投影（ClaudeRenderItem ⊇ GenericChatItem）。
 * status/complete 类 ClaudeMetaItem 不在时间线 state 里，由 #6 视图侧按 WS
 * 元事件追加，本层不产出。
 */
export function toClaudeRenderItems(view: ChatViewModel): ClaudeRenderItem[] {
  return view.items.map(viewItemToGeneric);
}

/**
 * Codex 频道渲染清单：ChatViewModel → CodexRenderItem[]。
 *
 * 同 Claude：复用 generic 投影；CodexMetaItem 由 #7 视图侧追加。
 */
export function toCodexRenderItems(view: ChatViewModel): CodexRenderItem[] {
  return view.items.map(viewItemToGeneric);
}
