import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalToastQueue } from "../ApprovalToastQueue";
import { useApprovalInboxStore } from "../useApprovalInbox";
import type { ApprovalInboxItem } from "@/protocol";

/**
 * smart-approval-manager-stage1 task #7：跨 channel 混合 Queue 排序。
 *
 * 验证 selectSorted 在 claude_code + generic_chat（+ 任意未来 channel）混合时仍按既有规则：
 * - danger 卡置顶
 * - 同组内按 arrivedAtMs 递增（旧 → 新）
 *
 * 这是「按钮显隐」改造之外的回归保险：channel 字段不参与排序。
 */

// 同既有 Queue 单测：CountdownBar 替换成壳，避免定时器
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
    blockedByRule: null,
    isElevated: false,
    danger: false,
    rememberAllowed: false,
    channel: "claude_code",
    cwd: "/p",
    arrivedAtMs: 0,
    timeoutMs: null,
    rememberRule: null,
    ...overrides,
  };
}

function seed(items: ApprovalInboxItem[]) {
  const byRequestId: Record<string, ApprovalInboxItem> = {};
  for (const i of items) byRequestId[i.requestId] = i;
  useApprovalInboxStore.setState({ byRequestId });
}

beforeEach(() => {
  useApprovalInboxStore.getState().clear();
});

describe("ApprovalToastQueue mixed-channel sorting", () => {
  it("混合 claude_code + generic_chat 安全卡按 arrivedAtMs 递增", () => {
    seed([
      makeItem("a", { channel: "claude_code", arrivedAtMs: 100 }),
      makeItem("b", { channel: "generic_chat", arrivedAtMs: 200 }),
      makeItem("c", { channel: "claude_code", arrivedAtMs: 300 }),
    ]);
    render(<ApprovalToastQueue />);
    const wrappers = screen.getAllByTestId("approval-inbox-card-wrapper");
    const order = wrappers.map((el) => el.getAttribute("data-request-id"));
    // channel 不参与排序，只看 arrivedAtMs 递增
    expect(order).toEqual(["a", "b", "c"]);
  });

  it("danger 卡跨 channel 仍置顶", () => {
    seed([
      makeItem("safe1", {
        channel: "generic_chat",
        arrivedAtMs: 100,
        blockedByRule: null,
      }),
      makeItem("danger", {
        channel: "claude_code",
        arrivedAtMs: 500,
        danger: true,
        blockedByRule: "Bash(rm:*)",
      }),
      makeItem("safe2", {
        channel: "generic_chat",
        arrivedAtMs: 200,
        blockedByRule: null,
      }),
    ]);
    render(<ApprovalToastQueue />);
    const wrappers = screen.getAllByTestId("approval-inbox-card-wrapper");
    const order = wrappers.map((el) => el.getAttribute("data-request-id"));
    // danger 置顶（无视 arrivedAtMs 较大且 channel 不同），safe 按 arrivedAtMs 递增
    expect(order).toEqual(["danger", "safe1", "safe2"]);
  });

  it("多 channel 多危险卡：危险组内 / 安全组内各自按 arrivedAtMs 递增", () => {
    seed([
      makeItem("safe-cc", {
        channel: "claude_code",
        arrivedAtMs: 50,
        blockedByRule: null,
      }),
      makeItem("danger-gc-late", {
        channel: "generic_chat",
        arrivedAtMs: 80,
        danger: true,
        blockedByRule: "rule-x",
      }),
      makeItem("safe-gc", {
        channel: "generic_chat",
        arrivedAtMs: 30,
        blockedByRule: null,
      }),
      makeItem("danger-cc-early", {
        channel: "claude_code",
        arrivedAtMs: 20,
        danger: true,
        blockedByRule: "rule-y",
      }),
    ]);
    render(<ApprovalToastQueue />);
    const wrappers = screen.getAllByTestId("approval-inbox-card-wrapper");
    const order = wrappers.map((el) => el.getAttribute("data-request-id"));
    // 危险组（danger-cc-early=20 < danger-gc-late=80）置顶，
    // 安全组（safe-gc=30 < safe-cc=50）跟随。
    expect(order).toEqual([
      "danger-cc-early",
      "danger-gc-late",
      "safe-gc",
      "safe-cc",
    ]);
  });
});
