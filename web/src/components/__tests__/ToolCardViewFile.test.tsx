import { render, screen } from "@testing-library/react";
import { describe, it, expect, beforeEach } from "vitest";
import { ChatMessageItem } from "@/components/MessageList";
import { useChatStore, type ChatItem } from "@/stores/chat";
import { useWorkspaceStore } from "@/stores/workspace";

/**
 * ToolCard "查看文件"按钮出现条件测试。
 *
 * 规则：按钮仅在 toolName==="write_file" && ok===true && resultData.path 为 string 时出现。
 */

function makeToolItem(
  overrides: Partial<Extract<ChatItem, { kind: "tool" }>> = {},
): Extract<ChatItem, { kind: "tool" }> {
  return {
    id: "tool-1",
    kind: "tool",
    threadId: "t1",
    turn: 1,
    runId: "run-1",
    toolName: "write_file",
    callId: "c1",
    arguments: { path: "/tmp/test.txt", content: "hello" },
    ok: true,
    result: "write 5 bytes -> /tmp/test.txt",
    resultData: { path: "/tmp/test.txt", bytes: 5, mode: "write" },
    timestampMs: Date.now(),
    ...overrides,
  };
}

beforeEach(() => {
  useChatStore.setState({ itemsByThread: {} });
  useWorkspaceStore.setState({
    contextsByThread: {
      t1: {
        thread_id: "t1",
        backend_kind: "generic_chat",
        workspace_root: "/tmp",
        claude_thread_id: "",
        shell_provider: "system_shell",
        files_available: true,
        shell_available: true,
      },
    },
    drawerFile: null,
  });
});

describe("ToolCard 查看文件按钮", () => {
  it("write_file 成功 + 有 path → 显示按钮", () => {
    render(<ChatMessageItem item={makeToolItem()} />);
    expect(screen.getByText("查看文件")).toBeInTheDocument();
  });

  it("write_file 失败(ok=false) → 不显示按钮", () => {
    render(
      <ChatMessageItem
        item={makeToolItem({ ok: false, errorMessage: "permission denied" })}
      />,
    );
    expect(screen.queryByText("查看文件")).not.toBeInTheDocument();
  });

  it("write_file 进行中(ok=null) → 不显示按钮", () => {
    render(
      <ChatMessageItem item={makeToolItem({ ok: null, result: undefined, resultData: undefined })} />,
    );
    expect(screen.queryByText("查看文件")).not.toBeInTheDocument();
  });

  it("read_file 成功 → 不显示按钮", () => {
    render(
      <ChatMessageItem
        item={makeToolItem({
          toolName: "read_file",
          resultData: { path: "/tmp/test.txt", bytes_read: 100, truncated: false, total_bytes: 100 },
        })}
      />,
    );
    expect(screen.queryByText("查看文件")).not.toBeInTheDocument();
  });

  it("list_dir 成功 → 不显示按钮", () => {
    render(
      <ChatMessageItem
        item={makeToolItem({
          toolName: "list_dir",
          resultData: { path: "/tmp", entries: [] },
        })}
      />,
    );
    expect(screen.queryByText("查看文件")).not.toBeInTheDocument();
  });

  it("write_file 成功但无 resultData → 不显示按钮", () => {
    render(
      <ChatMessageItem item={makeToolItem({ resultData: undefined })} />,
    );
    expect(screen.queryByText("查看文件")).not.toBeInTheDocument();
  });

  it("write_file 成功但 resultData.path 非字符串 → 不显示按钮", () => {
    render(
      <ChatMessageItem
        item={makeToolItem({ resultData: { bytes: 5, mode: "write" } })}
      />,
    );
    expect(screen.queryByText("查看文件")).not.toBeInTheDocument();
  });
});
