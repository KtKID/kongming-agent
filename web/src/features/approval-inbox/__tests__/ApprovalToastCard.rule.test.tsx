/** ApprovalToastCard 记忆规则交互测试。 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalInboxItem } from "@/protocol";
import { ApprovalToastCard } from "../ApprovalToastCard";
import { resetSender, setSender } from "../senderRef";
import { useApprovalInboxStore } from "../useApprovalInbox";

vi.mock("@/features/auto-approval", () => ({ CountdownBar: () => null }));

function makeItem(overrides: Partial<ApprovalInboxItem> = {}): ApprovalInboxItem {
  return {
    requestId: "req-rule",
    threadId: "thread-rule",
    toolName: "run_shell",
    toolInput: { command: "ls -la" },
    blockedByRule: null,
    isElevated: false,
    danger: false,
    rememberAllowed: true,
    channel: "generic_chat",
    cwd: "/workspace/a",
    arrivedAtMs: 1_000,
    timeoutMs: null,
    rememberRule: {
      expression: "run_shell(ls:*)",
      displayText: "允许 ls 开头的命令",
      scopeCwd: "/workspace/a",
    },
    ...overrides,
  };
}

beforeEach(() => {
  useApprovalInboxStore.getState().clear();
  resetSender();
});

describe("ApprovalToastCard remember", () => {
  it("展示服务端 canonical 候选规则", () => {
    render(<ApprovalToastCard item={makeItem()} />);
    expect(screen.getByTestId("approval-inbox-remember-display")).toHaveTextContent(
      "允许 ls 开头的命令",
    );
    expect(screen.getByTestId("approval-inbox-remember-expression")).toHaveTextContent(
      "run_shell(ls:*)",
    );
    expect(screen.getByTestId("approval-inbox-remember-cwd")).toHaveTextContent(
      "/workspace/a",
    );
  });

  it("rememberAllowed=true 且候选为空时只保留单次动作并提示不可记忆", () => {
    render(<ApprovalToastCard item={makeItem({ rememberRule: null })} />);
    expect(screen.getByTestId("approval-inbox-btn-allow-once")).toBeEnabled();
    expect(screen.queryByTestId("approval-inbox-btn-allow-remember")).toBeNull();
    expect(screen.queryByTestId("approval-inbox-btn-deny-remember")).toBeNull();
    expect(screen.getByTestId("approval-inbox-remember-unavailable")).toBeVisible();
  });

  it("服务端关闭 remember 时展示单次审批原因", () => {
    render(
      <ApprovalToastCard
        item={makeItem({ rememberAllowed: false, rememberRule: null })}
      />,
    );
    expect(screen.getByTestId("approval-inbox-remember-unavailable")).toHaveTextContent(
      "只支持单次审批",
    );
  });

  it("记忆请求未确认期间禁用两个记忆动作", () => {
    setSender(() => new Promise<boolean>(() => undefined));
    render(<ApprovalToastCard item={makeItem()} />);
    fireEvent.click(screen.getByTestId("approval-inbox-btn-allow-remember"));
    expect(screen.getByTestId("approval-inbox-btn-allow-remember")).toBeDisabled();
    expect(screen.getByTestId("approval-inbox-btn-deny-remember")).toBeDisabled();
    expect(screen.getByTestId("approval-inbox-remember-loading")).toBeVisible();
  });

  it("后端拒绝记忆写回时显示错误并保留 pending item", () => {
    const item = makeItem();
    setSender(() => true);
    useApprovalInboxStore
      .getState()
      .applyAddFrame({ frame_type: "approval.inbox.add", ...item });
    render(<ApprovalToastCard item={item} />);
    fireEvent.click(screen.getByTestId("approval-inbox-btn-deny-remember"));

    act(() => {
      useApprovalInboxStore.getState().applyResolveResultFrame({
        frame_type: "approval.inbox.resolve_result",
        requestId: item.requestId,
        accepted: false,
        message: "规则保存失败，请重试",
      });
    });

    expect(screen.getByTestId("approval-inbox-remember-error")).toBeVisible();
    expect(screen.queryByTestId("approval-inbox-remember-loading")).toBeNull();
    expect(useApprovalInboxStore.getState().byRequestId[item.requestId]).toBeDefined();
  });
});
