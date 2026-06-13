import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { MessageList } from "@/components/MessageList";
import { useChatStore } from "@/stores/chat";
import * as debugMod from "@/lib/debug";
import * as apiMod from "@/lib/api";

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

  it("渲染 user / assistant / tool / system / approval / error 6 类项", () => {
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
            usage: {
              prompt: 120,
              completion: 30,
              total: 150,
            },
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
            id: "sys1",
            kind: "system",
            threadId: "t1",
            runId: "run-1",
            noticeKey: "evolution:run-1",
            source: "self_evolution",
            status: "success",
            icon: "success",
            title: "进化复盘",
            message: "已沉淀 2 条进化养料",
            details: ["Consistent Short Response Cadence", "Predictable Instruction Execution Loop"],
            detailsData: {
              applied_written_count: 1,
              applied_skipped_count: 1,
              applied_failed_count: 0,
              applied_pending_count: 0,
            },
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
    expect(screen.getByTestId("system-notice-card")).toHaveTextContent("进化复盘");
    expect(screen.getByTestId("system-notice-card")).toHaveTextContent("已沉淀 2 条进化养料");
    expect(screen.getByText("已写入 1")).toBeInTheDocument();
    expect(screen.getByText("已命中 1")).toBeInTheDocument();
    expect(screen.getByText("Consistent Short Response Cadence")).toBeInTheDocument();
    expect(screen.getByText(/需要审批：Web/)).toBeInTheDocument();
    expect(screen.getByTestId("error-banner")).toHaveTextContent("boom");
    const footer = screen.getByTestId("assistant-usage-footer");
    expect(footer).toHaveTextContent("120");
    expect(footer).toHaveTextContent("30");
    expect(footer).toHaveTextContent("150");
  });

  it("鼠标进入气泡时显示复制和配置时区时间", () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(
      <MessageList
        threadId="t1"
        timezone="Asia/Shanghai"
        items={[
          {
            id: "u-hover",
            kind: "user",
            threadId: "t1",
            content: "hover-copy",
            timestampMs: Date.parse("2026-06-04T08:11:00.000Z"),
          },
        ]}
      />,
    );

    const meta = screen.getByTestId("message-hover-meta");
    expect(meta).toHaveClass("opacity-0");
    expect(meta).toHaveClass("group-hover:opacity-100");
    expect(meta).toHaveTextContent("16:11");

    fireEvent.click(screen.getByRole("button", { name: "复制消息" }));
    expect(writeText).toHaveBeenCalledWith("hover-copy");
  });

  it("系统提示卡片使用独立样式并显示失败原因", () => {
    useChatStore.setState({
      itemsByThread: {
        t1: [
          {
            id: "sys-fail",
            kind: "system",
            threadId: "t1",
            runId: "run-2",
            noticeKey: "evolution:run-2",
            source: "self_evolution",
            status: "error",
            icon: "error",
            title: "进化复盘",
            message: "本轮未写入",
            details: ["run_id must be a non-empty string"],
            timestampMs: 10,
          },
        ],
      },
    });

    render(<MessageList threadId="t1" />);

    const card = screen.getByTestId("system-notice-card");
    expect(card).toHaveTextContent("进化复盘");
    expect(card).toHaveTextContent("本轮未写入");
    expect(card).toHaveTextContent("run_id must be a non-empty string");
    expect(screen.queryByText("需要审批")).toBeNull();
  });

  it("成功态 evolution 卡片显示 CTA 并可打开弹窗", async () => {
    vi.spyOn(apiMod, "apiGetEvolutionReviews").mockResolvedValue([
      {
        review_id: "evo-review:run-1",
        run_id: "run-1",
        session_id: "t1",
        reviewed_at_ms: 10,
        review_summary: "captured one nutrient",
        nutrients: [
          {
            nutrient_id: "n1",
            kind: "workflow",
            title: "Workflow One",
            content: "content one",
            summary: "summary one",
            confidence: 0.9,
            evidence_turns: [1],
            source_run_id: "run-1",
            source_session_id: "t1",
            suggested_target: "skill",
            tags: [],
          },
        ],
        decision_summary: {
          total: 1,
          accepted_memory: 0,
          accepted_skill: 0,
          ignored: 0,
          pending: 1,
        },
        decisions: [],
      },
    ]);
    useChatStore.setState({
      itemsByThread: {
        t1: [
          {
            id: "sys1",
            kind: "system",
            threadId: "t1",
            runId: "run-1",
            noticeKey: "self_evolution.review",
            source: "self_evolution",
            status: "success",
            icon: "success",
            title: "进化复盘",
            message: "发现 1 条进化养料",
            details: ["pending_count: 1"],
            detailsData: {
              review_id: "evo-review:run-1",
              review_run_id: "run-1",
              session_id: "t1",
              write_status: "written",
            },
            timestampMs: 10,
          },
        ],
      },
    });

    render(<MessageList threadId="t1" />);
    fireEvent.click(screen.getByTestId("evolution-open-decision"));

    await waitFor(() => {
      expect(screen.getByText("处理进化养料")).toBeInTheDocument();
      expect(screen.getByText("Workflow One")).toBeInTheDocument();
      expect(screen.getByText("采纳为技能")).toBeInTheDocument();
    });
  });

  it("evolution 卡片显示 apply 进度 badge", () => {
    useChatStore.setState({
      itemsByThread: {
        t1: [
          {
            id: "sys-progress",
            kind: "system",
            threadId: "t1",
            runId: "run-3",
            noticeKey: "self_evolution.review",
            source: "self_evolution",
            status: "success",
            icon: "success",
            title: "进化复盘",
            message: "已写入 1/3 条进化养料，失败 1 条，待写入 1 条",
            details: ["pending_count: 0"],
            detailsData: {
              review_id: "evo-review:run-3",
              review_run_id: "run-3",
              session_id: "t1",
              applied_written_count: 1,
              applied_skipped_count: 0,
              applied_failed_count: 1,
              applied_pending_count: 1,
            },
            timestampMs: 10,
          },
        ],
      },
    });

    render(<MessageList threadId="t1" />);

    expect(screen.getByText("已写入 1")).toBeInTheDocument();
    expect(screen.getByText("失败 1")).toBeInTheDocument();
    expect(screen.getByText("待写入 1")).toBeInTheDocument();
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

  // chat-receive-side-unify #5：items 注入（generic 走时间线投影，不读 store）
  describe("items 注入 prop", () => {
    it("传 items 时渲染注入的清单，忽略 store", () => {
      // store 里放一条诱饵：若组件错读 store，会渲染 "store-only"。
      useChatStore.setState({
        itemsByThread: {
          t1: [
            {
              id: "store-decoy",
              kind: "user",
              threadId: "t1",
              content: "store-only",
              timestampMs: 1,
            },
          ],
        },
      });
      render(
        <MessageList
          threadId="t1"
          items={[
            {
              id: "inj-1",
              kind: "user",
              threadId: "t1",
              content: "injected-hi",
              timestampMs: 1,
            },
            {
              id: "inj-2",
              kind: "assistant",
              threadId: "t1",
              turn: 0,
              runId: "r1",
              content: "injected-answer",
              reasoning: "",
              timestampMs: 2,
              streaming: false,
            },
          ]}
        />,
      );
      // 注入项渲染
      expect(screen.getByText("injected-hi")).toBeInTheDocument();
      expect(screen.getByText("injected-answer")).toBeInTheDocument();
      // store 诱饵不渲染（证明确实没读 store）
      expect(screen.queryByText("store-only")).toBeNull();
    });

    it("不传 items 时退回读 store（其它频道现状不破）", () => {
      useChatStore.setState({
        itemsByThread: {
          t1: [
            {
              id: "store-1",
              kind: "user",
              threadId: "t1",
              content: "from-store",
              timestampMs: 1,
            },
          ],
        },
      });
      render(<MessageList threadId="t1" />);
      expect(screen.getByText("from-store")).toBeInTheDocument();
    });
  });
});
