import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, beforeEach } from "vitest";
import { ChatMessageItem } from "@/components/MessageList";
import { useChatStore, type ChatItem } from "@/stores/chat";
import { useWorkspaceStore } from "@/stores/workspace";

/**
 * ToolCard pending 模式渲染测试。
 *
 * 规则：
 * - pending=true 时折叠态显示 toolName + running badge；展开后显示 partialInput 而非 arguments
 * - pending=undefined / false 时走原有 arguments 显示路径
 * - "(尚无内容)" 占位在 partialInput 为空字符串 / undefined 时出现
 */

function makePendingTool(
  overrides: Partial<Extract<ChatItem, { kind: "tool" }>> = {},
): Extract<ChatItem, { kind: "tool" }> {
  return {
    id: "tool-pending-1",
    kind: "tool",
    threadId: "t1",
    turn: 1,
    runId: "run-1",
    toolName: "Agent",
    callId: "toolu_pending",
    arguments: {},
    ok: null,
    partialInput: "",
    pending: true,
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
        backend_kind: "claude_code",
        workspace_root: "/tmp",
        claude_thread_id: "sdk-x",
        shell_provider: "system_shell",
        files_available: false,
        shell_available: false,
      },
    },
    drawerFile: null,
  });
});

describe("ToolCard · pending 模式", () => {
  it("pending=true 折叠态：显示 toolName + running 状态", () => {
    render(<ChatMessageItem item={makePendingTool()} />);
    expect(screen.getByRole("button", { name: /Agent/ })).toBeInTheDocument();
    expect(screen.getByText(/running/i)).toBeInTheDocument();
    // 折叠态不显示 partialInput / arguments 区域
    expect(screen.queryByTestId("tool-partial-input")).toBeNull();
    expect(screen.queryByText("构建参数中…")).toBeNull();
  });

  it("pending=true 展开后：显示'构建参数中…' + partialInput", async () => {
    render(
      <ChatMessageItem
        item={makePendingTool({ partialInput: '{"k":"v"}' })}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Agent/ }));
    expect(screen.getByText("构建参数中…")).toBeInTheDocument();
    expect(screen.getByTestId("tool-partial-input")).toHaveTextContent(
      '{"k":"v"}',
    );
    // 不应显示 arguments 区段
    expect(screen.queryByText("arguments")).toBeNull();
  });

  it("pending=true partialInput 为空：占位显示 '(尚无内容)'", async () => {
    render(<ChatMessageItem item={makePendingTool({ partialInput: "" })} />);
    await userEvent.click(screen.getByRole("button", { name: /Agent/ }));
    expect(screen.getByTestId("tool-partial-input")).toHaveTextContent(
      "(尚无内容)",
    );
  });

  it("pending=false（resolve 后）展开：显示 arguments，不显示 partialInput", async () => {
    render(
      <ChatMessageItem
        item={makePendingTool({
          pending: false,
          partialInput: undefined,
          arguments: { description: "搜索", prompt: "x" },
        })}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Agent/ }));
    expect(screen.getByText("arguments")).toBeInTheDocument();
    expect(screen.getByText(/"description":\s*"搜索"/)).toBeInTheDocument();
    expect(screen.queryByTestId("tool-partial-input")).toBeNull();
    expect(screen.queryByText("构建参数中…")).toBeNull();
  });

  it("pending 字段缺失（历史回放路径）：等同 pending=false 走 arguments", async () => {
    const item = makePendingTool({
      pending: undefined,
      partialInput: undefined,
      arguments: { foo: "bar" },
    });
    render(<ChatMessageItem item={item} />);
    await userEvent.click(screen.getByRole("button", { name: /Agent/ }));
    expect(screen.getByText("arguments")).toBeInTheDocument();
    expect(screen.queryByText("构建参数中…")).toBeNull();
  });
});
