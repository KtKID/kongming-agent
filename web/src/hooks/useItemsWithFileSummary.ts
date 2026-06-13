import { useMemo, useRef } from "react";

/** 文件汇总虚拟项，插入到每轮 run 边界 */
export interface FilesSummaryItem {
  kind: "files-summary";
  id: string;
  files: string[];
  threadId: string;
}

/**
 * 从 tool item 提取被写入的文件路径。
 * 兼容三种频道的工具名：
 * - generic_chat: write_file → resultData.path
 * - claude_code:  Write / Edit / MultiEdit → arguments.file_path
 * - codex:        同 claude（codex 转成 GenericChatItem 格式）
 */
export function extractFilePath(item: unknown): string | null {
  if (typeof item !== "object" || item === null) return null;
  const obj = item as Record<string, unknown>;
  if (obj.kind !== "tool" || obj.ok !== true) return null;

  const toolName = obj.toolName as string | undefined;
  const resultData = obj.resultData as Record<string, unknown> | null | undefined;
  const args = obj.arguments as Record<string, unknown> | null | undefined;

  // generic_chat: write_file → resultData.path
  if (toolName === "write_file" && typeof resultData?.path === "string")
    return resultData.path as string;

  // claude_code / codex: Write / Edit / MultiEdit → arguments.file_path
  if (
    (toolName === "Write" || toolName === "Edit" || toolName === "MultiEdit") &&
    typeof args?.file_path === "string"
  )
    return args.file_path as string;

  return null;
}

/**
 * 通用 hook：按 user 消息分界，在每轮 LLM 回复末尾插入文件汇总虚拟项。
 * 三个频道（generic / claude / codex）共用，逻辑只写一次。
 *
 * @param items      原始消息数组
 * @param threadId   当前 thread ID（传入汇总卡片供 FileDrawer 使用）
 * @param isUserItem 调用方提供的判断函数，区分 user 消息（作为轮次分界）
 */
export function useItemsWithFileSummary<T>(
  items: T[],
  threadId: string | undefined,
  isUserItem: (item: T) => boolean,
): Array<T | FilesSummaryItem> {
  // 用 ref 持有回调，避免匿名函数引用变化导致 useMemo 每次重算
  // （参考项目 CLAUDE.md 记忆条目"React useEffect 不稳定回调死循环"）
  const isUserRef = useRef(isUserItem);
  isUserRef.current = isUserItem;

  return useMemo(() => {
    if (!threadId || items.length === 0) return items;

    const isUser = isUserRef.current;
    const result: Array<T | FilesSummaryItem> = [];
    let runFiles = new Map<string, string>();
    let inRun = false;
    let runIndex = 0;

    // 结算当前轮次：收集到的文件不为空时插入汇总卡片
    const flush = () => {
      if (runFiles.size > 0) {
        result.push({
          kind: "files-summary",
          id: `files-summary-${runIndex}`,
          files: Array.from(runFiles.keys()),
          threadId,
        });
      }
      runFiles = new Map();
      inRun = false;
    };

    for (let i = 0; i < items.length; i++) {
      const item = items[i] as T;

      // 遇到 user 消息 → 先结算上一轮，再推 user
      if (isUser(item)) {
        if (inRun) flush();
        result.push(item);
        inRun = true;
        runIndex++;
        continue;
      }

      result.push(item);

      // 收集文件路径（兼容所有频道工具名）
      const filePath = extractFilePath(item);
      if (filePath) {
        runFiles.set(filePath, filePath);
      }
    }

    // 末尾结算最后一轮
    if (inRun) flush();

    return result;
    // isUserRef 是 ref，不需要加入依赖数组
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, threadId]);
}
