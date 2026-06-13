import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { toast } from "sonner";
import { ApprovalDialog, type ApprovalAckSocket } from "@/components/ApprovalDialog";
import { useApprovalDialogStore } from "@/hooks/useApprovalDialog";

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
  },
}));

beforeEach(() => {
  useApprovalDialogStore.setState({ pending: [] });
  vi.mocked(toast.error).mockClear();
});

function pushApproval() {
  useApprovalDialogStore.getState().push({
    frame_type: "approval.request",
    timestamp_ms: 1,
    call_id: "c1",
    tool_name: "Shell",
    arguments: { cmd: "ls -la" },
    turn: 1,
  });
}

function pushElevatedApproval() {
  useApprovalDialogStore.getState().push({
    frame_type: "approval.request",
    timestamp_ms: 1,
    call_id: "c-elevated",
    tool_name: "run_shell",
    arguments: { command: "rm -rf .kongming/skills/musk-five-steps" },
    reason: "递归删除：最高优先级，必须审批",
    turn: 1,
    policy_hint: "elevated",
    confirm_token: "a1b2c3d4",
  });
}

function makeStubSocket() {
  return {
    send: vi.fn(),
  } as ApprovalAckSocket & { send: ReturnType<typeof vi.fn> };
}

describe("ApprovalDialog — standard 模式", () => {
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
      frame_type: "approval.ack",
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
      frame_type: "approval.ack",
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
      frame_type: "approval.ack",
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
      frame_type: "approval.ack",
      call_id: "c1",
      action: "reject",
    });
  });

  it("socket 为空时保留 approval 队列并提示重试", async () => {
    render(<ApprovalDialog socket={null} />);
    pushApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );

    await user.click(screen.getByTestId("approval-approve"));

    expect(useApprovalDialogStore.getState().pending.length).toBe(1);
    expect(toast.error).toHaveBeenCalledWith("审批连接未就绪，请稍后重试");
  });

  it("send 返回 false 时保留 approval 队列并提示重试", async () => {
    const sock = makeStubSocket();
    sock.send.mockReturnValue(false);
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );

    await user.click(screen.getByTestId("approval-approve"));

    expect(useApprovalDialogStore.getState().pending.length).toBe(1);
    expect(toast.error).toHaveBeenCalledWith("审批发送失败，请稍后重试");
  });

  it("send 抛错时保留 approval 队列并提示重试", async () => {
    const sock = makeStubSocket();
    sock.send.mockImplementation(() => {
      throw new Error("closed");
    });
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );

    await user.click(screen.getByTestId("approval-approve"));

    expect(useApprovalDialogStore.getState().pending.length).toBe(1);
    expect(toast.error).toHaveBeenCalledWith("审批发送失败，请稍后重试");
  });

  it("standard 模式显示「本 session 同意」按钮", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve-session")).toBeInTheDocument(),
    );
  });

  it("standard 模式无警告图标", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushApproval();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("elevated-warning-icon")).not.toBeInTheDocument();
  });
});

describe("ApprovalDialog — elevated 模式", () => {
  it("elevated 模式隐藏「本 session 同意」按钮", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("approval-approve-session")).not.toBeInTheDocument();
  });

  it("elevated 模式显示警告图标", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    await waitFor(() =>
      expect(screen.getByTestId("elevated-warning-icon")).toBeInTheDocument(),
    );
  });

  it("elevated 模式标题为「危险操作审批」", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    await waitFor(() =>
      expect(screen.getByText(/危险操作审批/)).toBeInTheDocument(),
    );
  });

  it("elevated 模式显示确认码输入区域", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    await waitFor(() =>
      expect(screen.getByTestId("confirm-token-area")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("confirm-token-input")).toBeInTheDocument();
  });

  it("elevated 模式未输入确认码时「同意」按钮禁用", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("approval-approve")).toBeDisabled();
  });

  it("elevated 模式输入正确确认码后「同意」按钮可用", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("confirm-token-input")).toBeInTheDocument(),
    );
    await user.type(screen.getByTestId("confirm-token-input"), "a1b2c3d4");
    expect(screen.getByTestId("approval-approve")).not.toBeDisabled();
  });

  it("elevated 模式输入错误确认码时「同意」按钮仍禁用", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("confirm-token-input")).toBeInTheDocument(),
    );
    await user.type(screen.getByTestId("confirm-token-input"), "wrong123");
    expect(screen.getByTestId("approval-approve")).toBeDisabled();
  });

  it("elevated 模式输入确认码后点同意 → 发送 accept_once", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("confirm-token-input")).toBeInTheDocument(),
    );
    await user.type(screen.getByTestId("confirm-token-input"), "a1b2c3d4");
    await user.click(screen.getByTestId("approval-approve"));
    expect(sock.send).toHaveBeenCalledWith({
      frame_type: "approval.ack",
      call_id: "c-elevated",
      action: "accept_once",
    });
  });

  it("elevated 模式「同意」按钮使用 destructive variant", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    await waitFor(() =>
      expect(screen.getByTestId("approval-approve")).toBeInTheDocument(),
    );
    // destructive variant 的 class 检查
    const btn = screen.getByTestId("approval-approve");
    // shadcn Button destructive variant 会有 bg-destructive 或类似 class
    // 至少验证它不是 default variant（无 outline）
    expect(btn.className).not.toContain("bg-primary");
  });

  it("elevated 拒绝仍可直接点击（不需要确认码）", async () => {
    const sock = makeStubSocket();
    render(<ApprovalDialog socket={sock} />);
    pushElevatedApproval();
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByTestId("approval-reject")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("approval-reject"));
    expect(sock.send).toHaveBeenCalledWith({
      frame_type: "approval.ack",
      call_id: "c-elevated",
      action: "reject",
    });
  });
});
