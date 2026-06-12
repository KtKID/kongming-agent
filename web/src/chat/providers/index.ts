/**
 * message-runtime-v0.1 · ChatProvider registry（#2 骨架）
 *
 * 按 provider 标识取对应实现。ChatManager 用 `getChatProvider(kind)` 分发，
 * 不直接 new 具体 provider，便于后续扩展新频道只在此登记一处。
 */
import type { ChatProvider, ChatProviderKind } from "@/chat/types";
import { GenericChatProvider } from "./GenericChatProvider";
import { ClaudeChatProvider } from "./ClaudeChatProvider";
import { CodexChatProvider } from "./CodexChatProvider";

const REGISTRY: Record<ChatProviderKind, ChatProvider> = {
  generic: new GenericChatProvider(),
  claude: new ClaudeChatProvider(),
  codex: new CodexChatProvider(),
};

/** 取指定频道的 provider 实现（单例）。 */
export function getChatProvider(kind: ChatProviderKind): ChatProvider {
  return REGISTRY[kind];
}

export { GenericChatProvider, ClaudeChatProvider, CodexChatProvider };
