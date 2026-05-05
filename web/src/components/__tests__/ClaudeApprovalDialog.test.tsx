import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import { ClaudeApprovalDialog } from "@/components/ClaudeApprovalDialog";

describe("ClaudeApprovalDialog", () => {
  const baseRequest = {
    requestId: "req-1",
    toolName: "Bash",
    toolInput: { command: "ls" },
  };

  it("显示工具名 + JSON 格式 toolInput", () => {
    render(
      <ClaudeApprovalDialog
        open={true}
        request={baseRequest}
        onResolve={vi.fn()}
      />,
    );
    expect(screen.getByText(/工具：Bash/)).toBeInTheDocument();
    expect(screen.getByText(/"command": "ls"/)).toBeInTheDocument();
  });

  it("点击「拒绝」回 allow=false + message=user denied", async () => {
    const onResolve = vi.fn();
    render(
      <ClaudeApprovalDialog
        open={true}
        request={baseRequest}
        onResolve={onResolve}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "拒绝" }));
    expect(onResolve).toHaveBeenCalledWith({
      requestId: "req-1",
      allow: false,
      message: "user denied",
    });
  });

  it("点击「单次允许」回 allow=true 不带 rememberEntry", async () => {
    const onResolve = vi.fn();
    render(
      <ClaudeApprovalDialog
        open={true}
        request={baseRequest}
        onResolve={onResolve}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "单次允许" }));
    expect(onResolve).toHaveBeenCalledWith({
      requestId: "req-1",
      allow: true,
    });
  });

  it("点击「本 session 都允许」回 allow=true + rememberEntry=toolName", async () => {
    const onResolve = vi.fn();
    render(
      <ClaudeApprovalDialog
        open={true}
        request={baseRequest}
        onResolve={onResolve}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "本 session 都允许" }),
    );
    expect(onResolve).toHaveBeenCalledWith({
      requestId: "req-1",
      allow: true,
      rememberEntry: "Bash",
    });
  });

  it("request=null 时不渲染（open=true 也不显示按钮）", () => {
    render(
      <ClaudeApprovalDialog open={true} request={null} onResolve={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "拒绝" })).toBeNull();
  });
});
