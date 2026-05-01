import { render, screen } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { MessageList } from "@/components/MessageList";
import { useChatStore } from "@/stores/chat";
import * as debugMod from "@/lib/debug";

beforeEach(() => {
  useChatStore.setState({ itemsByThread: {} });
});

describe("MessageList", () => {
  it("无 threadId → 显示提示", () => {
    render(<MessageList threadId={undefined} />);
    expect(screen.getByText(/在左侧选择/)).toBeInTheDocument();
  });

  it("空 thread → 引导文案", () => {
    render(<MessageList threadId="t1" />);
    expect(screen.getByText(/说点什么/)).toBeInTheDocument();
  });

  it("渲染 user / assistant / tool / approval / error 5 类项", () => {
    useChatStore.setState({
      itemsByThread: {
        t1: [
          {
            id: "u1",
            kind: "user",
            threadId: "t1",
            content: "hi",
            timestampMs: 1,
          },
          {
            id: "a1",
            kind: "assistant",
            threadId: "t1",
            turn: 1,
            runId: "",
            content: "hello world",
            reasoning: "",
            timestampMs: 2,
            streaming: false,
          },
          {
            id: "tool1",
            kind: "tool",
            threadId: "t1",
            turn: 1,
            runId: "",
            toolName: "Shell",
            callId: "c1",
            arguments: { cmd: "ls" },
            ok: true,
            timestampMs: 3,
          },
          {
            id: "ap1",
            kind: "approval",
            threadId: "t1",
            turn: 1,
            callId: "c2",
            toolName: "Web",
            arguments: {},
            timestampMs: 4,
          },
          {
            id: "e1",
            kind: "error",
            threadId: "t1",
            message: "boom",
            errorCode: "internal",
            timestampMs: 5,
          },
        ],
      },
    });
    render(<MessageList threadId="t1" />);
    expect(screen.getByText("hi")).toBeInTheDocument();
    expect(screen.getByText("hello world")).toBeInTheDocument();
    expect(screen.getByText("Shell")).toBeInTheDocument();
    expect(screen.getByText(/需要审批：Web/)).toBeInTheDocument();
    expect(screen.getByTestId("error-banner")).toHaveTextContent("boom");
  });

  // M5 debug badge
  describe("debug badge", () => {
    function _seedItems() {
      useChatStore.setState({
        itemsByThread: {
          t1: [
            {
              id: "assistant-t1-run-X-3",
              kind: "assistant",
              threadId: "t1",
              turn: 3,
              runId: "run-X",
              content: "hi",
              reasoning: "",
              timestampMs: 1,
              streaming: false,
            },
          ],
        },
      });
    }

    it("debug 关闭时 badge 不出现", () => {
      vi.spyOn(debugMod, "isDebugMode").mockReturnValue(false);
      _seedItems();
      render(<MessageList threadId="t1" />);
      expect(screen.queryByTestId("debug-badge")).toBeNull();
      vi.restoreAllMocks();
    });

    it("debug 开启时 badge 出现并含 id/runId/turn", () => {
      vi.spyOn(debugMod, "isDebugMode").mockReturnValue(true);
      _seedItems();
      render(<MessageList threadId="t1" />);
      const badge = screen.getByTestId("debug-badge");
      // id 前 8 = "assistan"; runId 后 6 = "run-X" (整串不足 6 取全)
      expect(badge.textContent).toContain("assistan");
      expect(badge.textContent).toContain("run-X");
      expect(badge.textContent).toContain("t3");
      vi.restoreAllMocks();
    });
  });
});
