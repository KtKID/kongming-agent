import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { ApprovalDialog } from "@/components/ApprovalDialog";
import { useApprovalDialogStore } from "@/hooks/useApprovalDialog";
import type { ThreadSocket } from "@/lib/ws";

beforeEach(() => {
  useApprovalDialogStore.setState({ pending: [] });
});

function pushApproval() {
  useApprovalDialogStore.getState().push({
    kind: "approval.request",
    timestamp_ms: 1,
    call_id: "c1",
    tool_name: "Shell",
    arguments: { cmd: "ls -la" },
    turn: 1,
  });
}

function makeStubSocket() {
  return {
    send: vi.fn(),
  } as unknown as ThreadSocket;
}

describe("ApprovalDialog", () => {
  it("无 pending 时不渲染", () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    expect(screen.queryByText(/需要审批/)).not.toBeInTheDocument();
  });

  it("点 '同意' → ws.send approval.ack(action=accept_once) + shift", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("approval-approve"));
    expect(sock.send).toHaveBeenCalledWith({
      kind: "approval.ack",
      call_id: "c1",
      action: "accept_once",
    });
    expect(useApprovalDialogStore.getState().pending.length).toBe(0);
  });

  it("审批参数区域启用自动换行，长内容留在弹窗内", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    const block = await screen.findByTestId("approval-arguments");
    expect(block).toHaveClass("whitespace-pre-wrap");
    expect(block).toHaveClass("break-all");
    expect(block).toHaveClass("overflow-x-hidden");
  });

  it("点 '本 session 同意' → action=accept_for_session", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(
        screen.getByTestId("approval-approve-session"),
      ).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("approval-approve-session"));
    expect(sock.send).toHaveBeenCalledWith({
      kind: "approval.ack",
      call_id: "c1",
      action: "accept_for_session",
    });
    expect(useApprovalDialogStore.getState().pending.length).toBe(0);
  });

  it("点 '拒绝' → action=reject", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("approval-reject")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("approval-reject"));
    expect(sock.send).toHaveBeenCalledWith({
      kind: "approval.ack",
      call_id: "c1",
      action: "reject",
    });
  });

  it("ESC 键 = 拒绝（action=reject）", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );
    const user = userEvent.setup();
    await user.keyboard("{Escape}");
    expect(sock.send).toHaveBeenCalledWith({
      kind: "approval.ack",
      call_id: "c1",
      action: "reject",
    });
  });
});
