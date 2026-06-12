import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ApprovalToastCard } from "../ApprovalToastCard";
import { useApprovalInboxStore } from "../useApprovalInbox";
import { setSender, resetSender } from "../senderRef";
import type { ApprovalInboxItem } from "../types";

// CountdownBar 真实组件会启定时器；这里 mock 成轻量壳，只暴露关键 data attr
vi.mock("@/features/auto-approval", () => ({
  CountdownBar: ({
    mode,
    deadlineMs,
  }: {
    mode?: string;
    deadlineMs: number;
    onComplete: () => void;
  }) => (
    <div
      data-testid="mock-countdown-bar"
      data-mode={mode ?? "approve"}
      data-deadline={deadlineMs}
    />
  ),
}));

function makeItem(overrides: Partial<ApprovalInboxItem> = {}): ApprovalInboxItem {
  return {
    requestId: "req-abc-12345",
    threadId: "thread-abcdef1234567890",
    toolName: "Bash",
    toolInput: { cmd: "ls -la" },
    autoApproveAtMs: null,
    autoRejectAtMs: null,
    blockedByRule: null,
    isElevated: false,
    channel: "claude_code",
    cwd: "/proj/x",
    arrivedAtMs: 1_000_000,
    timeoutMs: null,
    ...overrides,
  };
}

beforeEach(() => {
  useApprovalInboxStore.setState({ byRequestId: {} });
  resetSender();
});

describe("ApprovalToastCard 渲染", () => {
  it("渲染必要字段：toolName / thread short / toolInput preview", () => {
    render(<ApprovalToastCard item={makeItem()} />);
    expect(screen.getByTestId("approval-inbox-tool-name").textContent).toBe(
      "Bash",
    );
    // shortThreadId = 去 thread- 前缀后取前 8 位 hex（与 sitian sessionId 风格一致）
    expect(screen.getByTestId("approval-inbox-thread-short").textContent).toBe(
      "abcdef12",
    );
    expect(screen.getByTestId("approval-inbox-tool-input").textContent).toContain(
      "ls -la",
    );
  });

  it("三按钮 data-testid 都存在", () => {
    render(<ApprovalToastCard item={makeItem()} />);
    expect(screen.getByTestId("approval-inbox-btn-reject")).toBeInTheDocument();
    expect(screen.getByTestId("approval-inbox-btn-allow-once")).toBeInTheDocument();
    expect(
      screen.getByTestId("approval-inbox-btn-allow-session"),
    ).toBeInTheDocument();
  });

  it("普通卡：blockedByRule=null + isElevated=false → 无 destructive / 紫 Badge", () => {
    render(<ApprovalToastCard item={makeItem()} />);
    const card = screen.getByTestId("approval-inbox-card");
    expect(card.getAttribute("data-blocked")).toBe("0");
    expect(card.getAttribute("data-elevated")).toBe("0");
  });
});

describe("ApprovalToastCard 危险/elevated 状态", () => {
  it("危险卡：blockedByRule 非空 → data-blocked=1 + destructive Badge 显示规则 id", () => {
    render(
      <ApprovalToastCard
        item={makeItem({ blockedByRule: "rm-rf-root" })}
      />,
    );
    const card = screen.getByTestId("approval-inbox-card");
    expect(card.getAttribute("data-blocked")).toBe("1");
    expect(screen.getByText("rm-rf-root")).toBeInTheDocument();
  });

  it("elevated 卡：isElevated=true → data-elevated=1 + 紫 Badge 'elevated'", () => {
    render(<ApprovalToastCard item={makeItem({ isElevated: true })} />);
    const card = screen.getByTestId("approval-inbox-card");
    expect(card.getAttribute("data-elevated")).toBe("1");
    expect(screen.getByText("elevated")).toBeInTheDocument();
  });

  it("危险 + elevated 同时 → 显示危险 Badge（危险优先）", () => {
    render(
      <ApprovalToastCard
        item={makeItem({ blockedByRule: "rule-x", isElevated: true })}
      />,
    );
    expect(screen.getByText("rule-x")).toBeInTheDocument();
    // elevated 文本不应出现
    expect(screen.queryByText("elevated")).toBeNull();
  });
});

describe("ApprovalToastCard 倒计时", () => {
  it("autoApproveAtMs 非空 → CountdownBar mode=approve", () => {
    render(
      <ApprovalToastCard
        item={makeItem({ autoApproveAtMs: 9_999_999, autoRejectAtMs: null })}
      />,
    );
    const bar = screen.getByTestId("mock-countdown-bar");
    expect(bar.getAttribute("data-mode")).toBe("approve");
  });

  it("autoRejectAtMs 非空 → CountdownBar mode=reject", () => {
    render(
      <ApprovalToastCard
        item={makeItem({ autoApproveAtMs: null, autoRejectAtMs: 9_999_999 })}
      />,
    );
    const bar = screen.getByTestId("mock-countdown-bar");
    expect(bar.getAttribute("data-mode")).toBe("reject");
  });

  it("两者都 null（elevated）→ 不渲染倒计时", () => {
    render(
      <ApprovalToastCard
        item={makeItem({ autoApproveAtMs: null, autoRejectAtMs: null })}
      />,
    );
    expect(screen.queryByTestId("mock-countdown-bar")).toBeNull();
    expect(screen.queryByTestId("approval-inbox-countdown")).toBeNull();
  });

  // ---- v0.5 fallback 倒计时（smart-approval-generic-chat-autoallow task #2） ----

  it("fallback：autoApproveAtMs/autoRejectAtMs 都为 null + timeoutMs 非空 → 显示 reject 倒计时", () => {
    // 场景：generic_chat 未开 auto-approve 开关，后端走默认 60s timeout
    //   payload 只带 timeoutMs，autoApprove/RejectAtMs 都 null
    //   前端按 arrivedAtMs + timeoutMs 推 reject 倒计时（与后端 fail-closed 对齐）
    const arrivedAtMs = 1_700_000_000_000;
    const timeoutMs = 60_000;
    render(
      <ApprovalToastCard
        item={makeItem({
          autoApproveAtMs: null,
          autoRejectAtMs: null,
          arrivedAtMs,
          timeoutMs,
        })}
      />,
    );
    const bar = screen.getByTestId("mock-countdown-bar");
    // 必须是 reject 色调（绝不能误导用户以为会 approve）
    expect(bar.getAttribute("data-mode")).toBe("reject");
    // deadline = arrivedAtMs + timeoutMs
    expect(bar.getAttribute("data-deadline")).toBe(
      String(arrivedAtMs + timeoutMs),
    );
    // 包装容器也存在
    expect(
      screen.getByTestId("approval-inbox-countdown"),
    ).toBeInTheDocument();
  });

  it("fallback 不抢优先级：autoRejectAtMs 非空 + timeoutMs 非空 → 用 autoRejectAtMs 不走 fallback", () => {
    // 场景：危险规则命中 + 也透传了 timeoutMs；优先级 autoRejectAtMs > fallback
    //   deadline 必须等于 autoRejectAtMs，不能等于 arrivedAtMs+timeoutMs
    const arrivedAtMs = 1_700_000_000_000;
    const timeoutMs = 60_000;
    const autoRejectAtMs = 1_700_000_999_999; // 与 fallback 推算值刻意不同
    render(
      <ApprovalToastCard
        item={makeItem({
          autoApproveAtMs: null,
          autoRejectAtMs,
          arrivedAtMs,
          timeoutMs,
        })}
      />,
    );
    const bar = screen.getByTestId("mock-countdown-bar");
    expect(bar.getAttribute("data-mode")).toBe("reject");
    expect(bar.getAttribute("data-deadline")).toBe(String(autoRejectAtMs));
    // 验证没误用 fallback 值
    expect(bar.getAttribute("data-deadline")).not.toBe(
      String(arrivedAtMs + timeoutMs),
    );
  });

  it("fallback 不抢优先级：autoApproveAtMs 非空 + timeoutMs 非空 → 用 autoApproveAtMs 不走 fallback", () => {
    // 场景：auto-approve 开关 ON + 也透传 timeoutMs；优先级 autoApproveAtMs 最高
    const arrivedAtMs = 1_700_000_000_000;
    const timeoutMs = 60_000;
    const autoApproveAtMs = 1_700_000_010_000;
    render(
      <ApprovalToastCard
        item={makeItem({
          autoApproveAtMs,
          autoRejectAtMs: null,
          arrivedAtMs,
          timeoutMs,
        })}
      />,
    );
    const bar = screen.getByTestId("mock-countdown-bar");
    expect(bar.getAttribute("data-mode")).toBe("approve");
    expect(bar.getAttribute("data-deadline")).toBe(String(autoApproveAtMs));
  });

  it("fallback 边界：timeoutMs<=0 / arrivedAtMs<=0 不触发 fallback", () => {
    // timeoutMs=0 → 不算有效 timeout
    render(
      <ApprovalToastCard
        item={makeItem({
          autoApproveAtMs: null,
          autoRejectAtMs: null,
          arrivedAtMs: 1_700_000_000_000,
          timeoutMs: 0,
        })}
      />,
    );
    expect(screen.queryByTestId("mock-countdown-bar")).toBeNull();
  });

  it("approve 优先（v0.5 改）：两者都非 null → mode=approve", () => {
    // v0.5 smart-approval-generic-chat-autoallow 改约定：
    // 优先级 autoApproveAtMs > autoRejectAtMs > fallback
    // （README docs/safety-approval-manager-v0.5/ 倒计时表）
    render(
      <ApprovalToastCard
        item={makeItem({
          autoApproveAtMs: 9_999_999,
          autoRejectAtMs: 8_888_888,
        })}
      />,
    );
    const bar = screen.getByTestId("mock-countdown-bar");
    expect(bar.getAttribute("data-mode")).toBe("approve");
    // 用 approve 的 deadline
    expect(bar.getAttribute("data-deadline")).toBe("9999999");
  });
});

describe("ApprovalToastCard 按钮调 resolve", () => {
  it("reject 按钮 → resolve(threadId, requestId, false)", () => {
    const send = vi.fn();
    setSender(send);
    render(<ApprovalToastCard item={makeItem()} />);
    fireEvent.click(screen.getByTestId("approval-inbox-btn-reject"));
    expect(send).toHaveBeenCalledTimes(1);
    const frame = send.mock.calls[0][0];
    expect(frame.frame_type).toBe("approval.inbox.resolve");
    expect(frame.threadId).toBe("thread-abcdef1234567890");
    expect(frame.requestId).toBe("req-abc-12345");
    expect(frame.allow).toBe(false);
    // reject 带 message: "user denied"
    expect(frame.message).toBe("user denied");
    expect(frame.rememberEntry).toBeUndefined();
  });

  it("allow once 按钮 → resolve(threadId, requestId, true) 无 rememberEntry", () => {
    const send = vi.fn();
    setSender(send);
    render(<ApprovalToastCard item={makeItem()} />);
    fireEvent.click(screen.getByTestId("approval-inbox-btn-allow-once"));
    const frame = send.mock.calls[0][0];
    expect(frame.allow).toBe(true);
    expect("rememberEntry" in frame).toBe(false);
    expect("message" in frame).toBe(false);
  });

  it("allow session 按钮 → resolve(threadId, requestId, true, {rememberEntry: toolName, rememberScope: 'session'})", () => {
    const send = vi.fn();
    setSender(send);
    render(
      <ApprovalToastCard item={makeItem({ toolName: "ReadFile" })} />,
    );
    fireEvent.click(screen.getByTestId("approval-inbox-btn-allow-session"));
    const frame = send.mock.calls[0][0];
    expect(frame.allow).toBe(true);
    expect(frame.rememberEntry).toBe("ReadFile");
    // generic-chat-session-grant：必须带 rememberScope=session
    // 后端 ApprovalManager.resolve 检测此字段触发 add_session_grant
    expect(frame.rememberScope).toBe("session");
  });

  it("generic_chat 通道 allow session 按钮也带 rememberScope=session（fix-report-20260520）", () => {
    const send = vi.fn();
    setSender(send);
    render(
      <ApprovalToastCard
        item={makeItem({ toolName: "Bash", channel: "generic_chat" })}
      />,
    );
    // generic_chat 加入白名单后，第三按钮存在
    fireEvent.click(screen.getByTestId("approval-inbox-btn-allow-session"));
    const frame = send.mock.calls[0][0];
    expect(frame.frame_type).toBe("approval.inbox.resolve");
    expect(frame.allow).toBe(true);
    expect(frame.rememberEntry).toBe("Bash");
    expect(frame.rememberScope).toBe("session");
  });
});
