import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AutoApprovalModeSelector } from "../AutoApprovalToggle";
import { useAutoApprovalStore } from "../useAutoApproval";

beforeEach(() => {
  useAutoApprovalStore.getState().clear();
});

describe("AutoApprovalModeSelector", () => {
  it("缺少 cwd 或 socket 时不渲染", () => {
    expect(render(<AutoApprovalModeSelector socket={{ send: vi.fn() }} />).container.firstChild).toBeNull();
    expect(render(<AutoApprovalModeSelector cwd="/p" socket={null} />).container.firstChild).toBeNull();
  });

  it("挂载查询模式，并以 user 作为保守默认值", () => {
    const send = vi.fn();
    render(<AutoApprovalModeSelector cwd="/p" socket={{ send }} />);
    expect(send).toHaveBeenCalledWith({ frame_type: "auto-approval-query", cwd: "/p" });
    expect((screen.getByTestId("approval-mode-select") as HTMLSelectElement).value).toBe("user");
  });

  it("选择 LLM 与完全信任时发送三态协议值", () => {
    const send = vi.fn();
    render(<AutoApprovalModeSelector cwd="/p" socket={{ send }} />);
    send.mockClear();
    fireEvent.change(screen.getByTestId("approval-mode-select"), { target: { value: "llm" } });
    fireEvent.change(screen.getByTestId("approval-mode-select"), { target: { value: "full_trust" } });
    expect(send).toHaveBeenNthCalledWith(1, { frame_type: "auto-approval-set-mode", cwd: "/p", mode: "llm" });
    expect(send).toHaveBeenNthCalledWith(2, { frame_type: "auto-approval-set-mode", cwd: "/p", mode: "full_trust" });
  });
});
