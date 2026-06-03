import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalToastCard } from "../ApprovalToastCard";
import { useApprovalInboxStore } from "../useApprovalInbox";
import { resetSender } from "../senderRef";
import type { ApprovalInboxItem } from "../types";

/**
 * smart-approval-manager-stage1 task #7：channel 控制「本 session」按钮显隐。
 *
 * 设计要点（与 ApprovalToastCard 内 CHANNELS_WITH_SESSION_GRANT 白名单对齐）：
 * - 白名单内（claude_code）→ 渲染三按钮
 * - 白名单外（generic_chat / 未知 channel）→ 只渲染两按钮
 *
 * 验证抓手：复用现有 data-testid（approval-inbox-btn-reject / -allow-once / -allow-session）。
 */

// 同 Card 既有单测：避免 CountdownBar 定时器
vi.mock("@/features/auto-approval", () => ({
  CountdownBar: () => <div data-testid="mock-countdown-bar" />,
}));

function makeItem(overrides: Partial<ApprovalInboxItem> = {}): ApprovalInboxItem {
  return {
    requestId: "req-channel-test",
    threadId: "thread-channel-test-xyz",
    toolName: "Bash",
    toolInput: { cmd: "ls" },
    autoApproveAtMs: null,
    autoRejectAtMs: null,
    blockedByRule: null,
    isElevated: false,
    channel: "claude_code",
    cwd: "/tmp",
    arrivedAtMs: 1_000,
    timeoutMs: null,
    ...overrides,
  };
}

beforeEach(() => {
  useApprovalInboxStore.setState({ byRequestId: {} });
  resetSender();
});

describe("ApprovalToastCard channel-based button visibility", () => {
  it("claude_code 通道显示三按钮（白名单内）", () => {
    render(<ApprovalToastCard item={makeItem({ channel: "claude_code" })} />);
    expect(
      screen.getByTestId("approval-inbox-btn-reject"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("approval-inbox-btn-allow-once"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("approval-inbox-btn-allow-session"),
    ).toBeInTheDocument();
  });

  it("generic_chat 通道显示三按钮（fix-report-20260520-generic-chat-session-grant 后已加入白名单）", () => {
    render(<ApprovalToastCard item={makeItem({ channel: "generic_chat" })} />);
    expect(
      screen.getByTestId("approval-inbox-btn-reject"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("approval-inbox-btn-allow-once"),
    ).toBeInTheDocument();
    // 本 session 现在也显示（生效后真正写 ApprovalRules._thread_overrides）
    expect(
      screen.getByTestId("approval-inbox-btn-allow-session"),
    ).toBeInTheDocument();
  });

  it("未知 channel（如 cron）保守隐藏「本 session」按钮（白名单外）", () => {
    render(<ApprovalToastCard item={makeItem({ channel: "cron" })} />);
    expect(
      screen.queryByTestId("approval-inbox-btn-allow-session"),
    ).not.toBeInTheDocument();
    // 二态仍可用，确保 cron 仍能正常决议（只是不能 remember-session）
    expect(
      screen.getByTestId("approval-inbox-btn-reject"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("approval-inbox-btn-allow-once"),
    ).toBeInTheDocument();
  });

  it("空字符串 channel 保守隐藏「本 session」按钮", () => {
    render(<ApprovalToastCard item={makeItem({ channel: "" })} />);
    expect(
      screen.queryByTestId("approval-inbox-btn-allow-session"),
    ).not.toBeInTheDocument();
  });
});
