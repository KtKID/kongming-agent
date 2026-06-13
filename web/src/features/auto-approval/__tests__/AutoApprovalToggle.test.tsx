import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AutoApprovalToggle } from "../AutoApprovalToggle";
import { useAutoApprovalStore } from "../useAutoApproval";

beforeEach(() => {
  useAutoApprovalStore.getState().clear();
});

describe("AutoApprovalToggle", () => {
  it("没传 cwd → 不渲染", () => {
    const { container } = render(
      <AutoApprovalToggle cwd={undefined} socket={{ send: vi.fn() }} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("没传 socket → 不渲染", () => {
    const { container } = render(
      <AutoApprovalToggle cwd="/p" socket={null} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("挂载时主动 query 后端状态", () => {
    const send = vi.fn();
    render(<AutoApprovalToggle cwd="/p" socket={{ send }} />);
    expect(send).toHaveBeenCalledWith({ frame_type: "auto-approval-query", cwd: "/p" });
  });

  it("默认状态（store 没条目）显示 enabled=false", () => {
    render(<AutoApprovalToggle cwd="/p" socket={{ send: vi.fn() }} />);
    const sw = screen.getByTestId("auto-approval-switch") as HTMLButtonElement;
    expect(sw.getAttribute("aria-checked")).toBe("false");
  });

  it("store 已有启用状态时显示 enabled + 秒数", () => {
    useAutoApprovalStore.getState().applyStateFrame({
      frame_type: "auto_approval_state",
      channel: "claude_code",
      cwd: "/p",
      enabled: true,
      timeoutMs: 8000,
      ruleOverrides: {},
    });
    render(<AutoApprovalToggle cwd="/p" socket={{ send: vi.fn() }} />);
    const sw = screen.getByTestId("auto-approval-switch") as HTMLButtonElement;
    expect(sw.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("8s")).toBeTruthy();
  });

  it("点 switch 时发 toggle 帧", () => {
    const send = vi.fn();
    render(<AutoApprovalToggle cwd="/p" socket={{ send }} />);
    // 清掉 mount 时的 query
    send.mockClear();
    const sw = screen.getByTestId("auto-approval-switch");
    fireEvent.click(sw);
    expect(send).toHaveBeenCalledWith({
      frame_type: "auto-approval-toggle",
      cwd: "/p",
      enabled: true,
    });
  });

  it("cwd 变化时重新 query", () => {
    const send = vi.fn();
    const { rerender } = render(
      <AutoApprovalToggle cwd="/p1" socket={{ send }} />,
    );
    send.mockClear();
    rerender(<AutoApprovalToggle cwd="/p2" socket={{ send }} />);
    expect(send).toHaveBeenCalledWith({ frame_type: "auto-approval-query", cwd: "/p2" });
  });
});
