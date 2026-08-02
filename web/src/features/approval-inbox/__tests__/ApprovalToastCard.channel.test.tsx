/** ApprovalToastCard 按服务端 rememberAllowed 渲染，channel 不参与本地安全判断。 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApprovalInboxItem } from "@/protocol";
import { ApprovalToastCard } from "../ApprovalToastCard";

vi.mock("@/features/auto-approval", () => ({ CountdownBar: () => null }));

function makeItem(overrides: Partial<ApprovalInboxItem> = {}): ApprovalInboxItem {
  return {
    requestId: "req-channel",
    threadId: "thread-channel",
    toolName: "read_file",
    toolInput: { path: "README.md" },
    blockedByRule: null,
    isElevated: false,
    danger: false,
    rememberAllowed: true,
    channel: "generic_chat",
    cwd: "/workspace",
    arrivedAtMs: 1_000,
    timeoutMs: null,
    rememberRule: {
      expression: "read_file(README.md)",
      displayText: "允许读取 README.md",
      scopeCwd: null,
    },
    ...overrides,
  };
}

describe("ApprovalToastCard remember capability", () => {
  it.each(["generic_chat", "claude_code", "cron"])(
    "%s 通道在 rememberAllowed=true 时显示两个记忆动作",
    (channel) => {
      render(<ApprovalToastCard item={makeItem({ channel })} />);
      expect(screen.getByTestId("approval-inbox-btn-allow-remember")).toBeInTheDocument();
      expect(screen.getByTestId("approval-inbox-btn-deny-remember")).toBeInTheDocument();
    },
  );

  it("rememberAllowed=false 时隐藏两个记忆动作", () => {
    render(
      <ApprovalToastCard
        item={makeItem({ rememberAllowed: false, rememberRule: null })}
      />,
    );
    expect(screen.queryByTestId("approval-inbox-btn-allow-remember")).toBeNull();
    expect(screen.queryByTestId("approval-inbox-btn-deny-remember")).toBeNull();
  });
});
