import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ClaudeCodeView } from "@/components/ClaudeCodeView";
import type { NormalizedMessage, ThreadMetadataDTO } from "@/protocol";

/**
 * 测试 input_json delta 不再漏到 assistant 文字气泡，而是累积到独立 pending ToolCard。
 *
 * 验证场景：
 * - (a) stream_delta(deltaType=input_json) 不进 assistant.content
 * - (b) stream_status(phase=tool_calling) 创建 pending ToolItem（折叠 + running badge）
 * - (c) tool_use 帧按 toolId 匹配 pending → resolve（同一 ChatItem 原地更新，不增不减）
 * - (d) 兜底：stream_delta(input_json) 找不到 pending ToolItem 时静默丢弃，不报错、不污染 assistant
 * - (e) 历史回放（jsonl_history）走 convertHistoryToChatItems 路径，不会产出 pending=true
 */

const mockApiGet = vi.fn();
const mockUseClaudeCodeWS = vi.fn();

vi.mock("@/lib/api", () => ({
  apiGet: (...args: unknown[]) => mockApiGet(...args),
}));

vi.mock("@/hooks/useClaudeCodeWS", () => ({
  useClaudeCodeWS: (...args: unknown[]) => mockUseClaudeCodeWS(...args),
}));

interface SocketHandle {
  socket: { on: ReturnType<typeof vi.fn>; send: ReturnType<typeof vi.fn> };
  inject: (frame: NormalizedMessage) => void;
}

function makeSocket(): SocketHandle {
  let listener: ((frame: NormalizedMessage) => void) | null = null;
  const socket = {
    on: vi.fn((cb: (frame: NormalizedMessage) => void) => {
      listener = cb;
      return () => {
        listener = null;
      };
    }),
    send: vi.fn(),
  };
  return {
    socket,
    inject: (frame) => {
      act(() => {
        listener?.(frame);
      });
    },
  };
}

async function mountView(handle: SocketHandle, history: NormalizedMessage[] = []) {
  mockUseClaudeCodeWS.mockReturnValue({
    socket: handle.socket,
    state: "open",
  });
  mockApiGet.mockResolvedValue({ messages: history });

  render(
    <MemoryRouter>
      <ClaudeCodeView
        threadId="thread-1"
        thread={{ claude_thread_id: "sdk-1" } as ThreadMetadataDTO}
      />
    </MemoryRouter>,
  );

  await waitFor(() =>
    expect(handle.socket.on).toHaveBeenCalled(),
  );
}

describe("ClaudeCodeView · input_json pending tool card", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockUseClaudeCodeWS.mockReset();
  });

  it("(a)(b)(c) stream_status → pending ToolCard；input_json delta 累积；tool_use resolve 同 ChatItem 原地更新", async () => {
    const handle = makeSocket();
    await mountView(handle);

    // (b) tool_calling block 起始 → 出现一张 pending ToolCard
    handle.inject({
      frame_type: "stream_status",
      phase: "tool_calling",
      toolName: "Agent",
      toolId: "toolu_01",
    } as unknown as NormalizedMessage);

    const beforeButton = await screen.findByRole("button", { name: /Agent/ });
    expect(beforeButton).toBeInTheDocument();

    // 展开看 partialInput 区域
    await userEvent.click(beforeButton);
    expect(screen.getByText("构建参数中…")).toBeInTheDocument();
    expect(screen.getByTestId("tool-partial-input")).toHaveTextContent(
      "(尚无内容)",
    );

    // (a) input_json delta 流式累积到 partialInput；不创建 assistant 气泡
    handle.inject({
      frame_type: "stream_delta",
      deltaType: "input_json",
      content: '{"description":"搜索",',
      toolId: "toolu_01",
    } as unknown as NormalizedMessage);
    handle.inject({
      frame_type: "stream_delta",
      deltaType: "input_json",
      content: '"prompt":"x"}',
      toolId: "toolu_01",
    } as unknown as NormalizedMessage);

    expect(screen.getByTestId("tool-partial-input")).toHaveTextContent(
      '{"description":"搜索","prompt":"x"}',
    );
    // 关键断言：assistant 气泡里不应出现 raw JSON
    expect(
      screen.queryByText(/"description":"搜索"/i, { selector: "p, span" }),
    ).toBeNull();

    // 仍然只有 1 张 ToolCard（用 toolName 按钮数量代理）
    expect(screen.getAllByRole("button", { name: /Agent/ })).toHaveLength(1);

    // (c) tool_use 帧 → 按 toolId resolve；ToolCard 仍然只有 1 张，partialInput 区域消失
    handle.inject({
      frame_type: "tool_use",
      toolName: "Agent",
      toolId: "toolu_01",
      toolInput: { description: "搜索", prompt: "x" },
    } as unknown as NormalizedMessage);

    await waitFor(() => {
      expect(screen.queryByText("构建参数中…")).toBeNull();
    });
    expect(screen.getAllByRole("button", { name: /Agent/ })).toHaveLength(1);
    // 现在显示 arguments 而非 partialInput
    expect(screen.queryByTestId("tool-partial-input")).toBeNull();
    expect(screen.getByText(/"description"/)).toBeInTheDocument();
  });

  it("(d) 兜底：stream_delta(input_json) 找不到 pending ToolItem 时静默丢弃，不污染 assistant", async () => {
    const handle = makeSocket();
    await mountView(handle);

    // 没有 stream_status 创建 pending → 直接来 input_json delta
    handle.inject({
      frame_type: "stream_delta",
      deltaType: "input_json",
      content: '{"orphan":true}',
      toolId: "toolu_lost",
    } as unknown as NormalizedMessage);

    // 不应抛错（已通过执行到这里证明）
    // 不应创建 ToolCard
    expect(screen.queryByRole("button", { name: /\(未知工具\)/ })).toBeNull();
    // 不应创建 assistant 气泡
    expect(screen.queryByText(/orphan/)).toBeNull();
  });

  it("(a' 对照) text delta 仍然走 assistant 气泡（不被 input_json 分支误伤）", async () => {
    const handle = makeSocket();
    await mountView(handle);

    handle.inject({
      frame_type: "stream_delta",
      deltaType: "text",
      content: "你好",
    } as unknown as NormalizedMessage);
    handle.inject({
      frame_type: "stream_delta",
      deltaType: "text",
      content: "世界",
    } as unknown as NormalizedMessage);

    expect(await screen.findByText("你好世界")).toBeInTheDocument();
  });

  it("(e) 历史回放路径不产出 pending=true 的 ToolItem", async () => {
    const handle = makeSocket();
    const history: NormalizedMessage[] = [
      {
        frame_type: "tool_use",
        toolId: "tool-h1",
        toolName: "Read",
        toolInput: { path: "/a" },
        timestamp: "2026-05-02T00:00:01Z",
      },
      {
        frame_type: "tool_result",
        toolId: "tool-h1",
        content: "hello",
        timestamp: "2026-05-02T00:00:02Z",
      },
    ];
    await mountView(handle, history);

    const toolButton = await screen.findByRole("button", { name: /Read/ });
    await userEvent.click(toolButton);

    // 历史 ToolItem 默认 pending=undefined（隐式 false）→ 显示 arguments，不显示"构建参数中"
    expect(screen.queryByText("构建参数中…")).toBeNull();
    expect(screen.getByText(/"path":\s*"\/a"/)).toBeInTheDocument();
  });
});
