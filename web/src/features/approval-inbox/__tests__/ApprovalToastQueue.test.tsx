import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalToastQueue } from "../ApprovalToastQueue";
import { useApprovalInboxStore } from "../useApprovalInbox";
import type { ApprovalInboxItem } from "../types";

// 同 Card 单测：CountdownBar 替换成壳，避免定时器
vi.mock("@/features/auto-approval", () => ({
  CountdownBar: () => <div data-testid="mock-countdown-bar" />,
}));

function makeItem(
  requestId: string,
  overrides: Partial<ApprovalInboxItem> = {},
): ApprovalInboxItem {
  return {
    requestId,
    threadId: `thread-${requestId}`,
    toolName: "Bash",
    toolInput: { cmd: "x" },
    autoApproveAtMs: null,
    autoRejectAtMs: null,
    blockedByRule: null,
    isElevated: false,
    channel: "claude_code",
    cwd: "/p",
    arrivedAtMs: 0,
    timeoutMs: null,
    ...overrides,
  };
}

function seed(items: ApprovalInboxItem[]) {
  const byRequestId: Record<string, ApprovalInboxItem> = {};
  for (const i of items) byRequestId[i.requestId] = i;
  useApprovalInboxStore.setState({ byRequestId });
}

beforeEach(() => {
  useApprovalInboxStore.setState({ byRequestId: {} });
});

describe("ApprovalToastQueue 空态", () => {
  it("空 byRequestId → 不渲染容器", () => {
    const { container } = render(<ApprovalToastQueue />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("approval-inbox-queue")).toBeNull();
  });
});

describe("ApprovalToastQueue 排序与渲染", () => {
  it("非空 → 渲染容器 + data-count", () => {
    seed([makeItem("r1"), makeItem("r2")]);
    render(<ApprovalToastQueue />);
    const queue = screen.getByTestId("approval-inbox-queue");
    expect(queue).toBeInTheDocument();
    expect(queue.getAttribute("data-count")).toBe("2");
  });

  it("排序：危险卡置顶 + 同组按 arrivedAtMs 递增", () => {
    seed([
      makeItem("safe-late", { arrivedAtMs: 50 }),
      makeItem("danger-late", { blockedByRule: "r1", arrivedAtMs: 40 }),
      makeItem("safe-early", { arrivedAtMs: 10 }),
      makeItem("danger-early", { blockedByRule: "r2", arrivedAtMs: 20 }),
    ]);
    render(<ApprovalToastQueue />);
    const wrappers = screen.getAllByTestId("approval-inbox-card-wrapper");
    const order = wrappers.map((el) => el.getAttribute("data-request-id"));
    expect(order).toEqual([
      "danger-early",
      "danger-late",
      "safe-early",
      "safe-late",
    ]);
  });
});

describe("ApprovalToastQueue 上限", () => {
  it("60 条 → 显示 50 张 card + +10 more 折叠", () => {
    const items: ApprovalInboxItem[] = [];
    for (let i = 0; i < 60; i++) {
      items.push(makeItem(`r${i.toString().padStart(2, "0")}`, { arrivedAtMs: i }));
    }
    seed(items);
    render(<ApprovalToastQueue />);
    const wrappers = screen.getAllByTestId("approval-inbox-card-wrapper");
    expect(wrappers.length).toBe(50);
    const overflow = screen.getByTestId("approval-inbox-overflow");
    expect(overflow.textContent).toContain("+10");
  });

  it("50 条 → 不显示 +N more", () => {
    const items: ApprovalInboxItem[] = [];
    for (let i = 0; i < 50; i++) {
      items.push(makeItem(`r${i}`, { arrivedAtMs: i }));
    }
    seed(items);
    render(<ApprovalToastQueue />);
    expect(screen.queryByTestId("approval-inbox-overflow")).toBeNull();
    expect(screen.getAllByTestId("approval-inbox-card-wrapper").length).toBe(50);
  });
});

describe("ApprovalToastQueue 动画 marker（不验 transform）", () => {
  it("每张卡片有 data-testid=approval-inbox-card-wrapper", () => {
    seed([makeItem("r1"), makeItem("r2")]);
    render(<ApprovalToastQueue />);
    const wrappers = screen.getAllByTestId("approval-inbox-card-wrapper");
    // 按 spec 要求：仅验 data-testid + 类名出现，不验 transform 数值
    expect(wrappers.length).toBe(2);
    wrappers.forEach((w) => {
      expect(w.getAttribute("data-request-id")).toBeTruthy();
    });
  });
});
