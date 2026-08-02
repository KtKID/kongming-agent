/** ApprovalToastCard v0.6 交互测试：四动作、danger 强提示与 fail-closed timeout。 */

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalInboxItem } from "@/protocol";
import { ApprovalToastCard } from "../ApprovalToastCard";
import { resetSender, setSender } from "../senderRef";
import { useApprovalInboxStore } from "../useApprovalInbox";

vi.mock("@/features/auto-approval", () => ({
  CountdownBar: ({ onComplete }: { onComplete: () => void }) => (
    <button type="button" data-testid="mock-countdown-bar" onClick={onComplete} />
  ),
}));

function makeItem(overrides: Partial<ApprovalInboxItem> = {}): ApprovalInboxItem {
  return {
    requestId: "req-card",
    threadId: "thread-abcdef123456",
    toolName: "run_shell",
    toolInput: { command: "ls -la" },
    blockedByRule: null,
    isElevated: false,
    danger: false,
    rememberAllowed: true,
    channel: "generic_chat",
    cwd: "/workspace",
    arrivedAtMs: 1_000,
    timeoutMs: null,
    rememberRule: {
      expression: "run_shell(ls:*)",
      displayText: "允许 ls 开头的命令",
      scopeCwd: "/workspace",
    },
    ...overrides,
  };
}

beforeEach(() => {
  useApprovalInboxStore.getState().clear();
  resetSender();
});

describe("ApprovalToastCard", () => {
  it("渲染工具、thread、输入预览和普通卡四个动作", () => {
    render(<ApprovalToastCard item={makeItem()} />);
    expect(screen.getByTestId("approval-inbox-tool-name")).toHaveTextContent("run_shell");
    expect(screen.getByTestId("approval-inbox-thread-short")).toHaveTextContent("abcdef12");
    expect(screen.getByTestId("approval-inbox-tool-input")).toHaveTextContent("ls -la");
    expect(screen.getByRole("button", { name: "允许一次" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "允许并记住" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "拒绝一次" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "拒绝并记住" })).toBeInTheDocument();
  });

  it.each([
    ["approval-inbox-btn-allow-once", true, false, undefined],
    ["approval-inbox-btn-allow-remember", true, true, undefined],
    ["approval-inbox-btn-reject", false, false, "user denied"],
    ["approval-inbox-btn-deny-remember", false, true, undefined],
  ] as const)("动作 %s 发送 allow=%s remember=%s", (testId, allow, remember, message) => {
    const send = vi.fn(() => true);
    setSender(send);
    render(<ApprovalToastCard item={makeItem()} />);

    fireEvent.click(screen.getByTestId(testId));

    expect(send).toHaveBeenCalledWith({
      frame_type: "approval.inbox.resolve",
      threadId: "thread-abcdef123456",
      requestId: "req-card",
      allow,
      remember,
      ...(remember ? { rememberRule: makeItem().rememberRule } : {}),
      ...(message === undefined ? {} : { message }),
    });
  });

  it("danger 卡使用强红视觉、隐藏记忆动作并拦截 Enter 快捷确认", () => {
    const send = vi.fn(() => true);
    setSender(send);
    render(
      <ApprovalToastCard
        item={makeItem({
          danger: true,
          rememberAllowed: true,
          blockedByRule: "danger.delete-root",
        })}
      />,
    );

    const card = screen.getByTestId("approval-inbox-card");
    expect(card).toHaveClass("border-destructive", "bg-destructive/10");
    expect(screen.getByText("danger.delete-root")).toBeInTheDocument();
    expect(screen.queryByTestId("approval-inbox-btn-allow-remember")).toBeNull();
    expect(screen.queryByTestId("approval-inbox-btn-deny-remember")).toBeNull();

    fireEvent.keyDown(screen.getByTestId("approval-inbox-btn-allow-once"), {
      key: "Enter",
      code: "Enter",
    });
    expect(send).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("approval-inbox-btn-allow-once"));
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("人工审批 timeout 只发送拒绝", () => {
    const send = vi.fn(() => true);
    setSender(send);
    render(<ApprovalToastCard item={makeItem({ timeoutMs: 60_000 })} />);
    fireEvent.click(screen.getByTestId("mock-countdown-bar"));
    expect(send).toHaveBeenCalledWith({
      frame_type: "approval.inbox.resolve",
      threadId: "thread-abcdef123456",
      requestId: "req-card",
      allow: false,
      remember: false,
      message: "approval timeout",
    });
  });
});
