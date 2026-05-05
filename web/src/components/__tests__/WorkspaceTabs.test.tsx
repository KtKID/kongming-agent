import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { WorkspaceTabs } from "@/components/WorkspaceTabs";

describe("WorkspaceTabs", () => {
  it("渲染 Chat / Files / Git / Shell 四个 tab", () => {
    render(<WorkspaceTabs active="chat" onChange={vi.fn()} />);
    expect(screen.getByText("Chat")).toBeInTheDocument();
    expect(screen.getByText("Files")).toBeInTheDocument();
    expect(screen.getByText("Git")).toBeInTheDocument();
    expect(screen.getByText("Shell")).toBeInTheDocument();
  });

  it("点击 Git 触发 onChange('git')", async () => {
    const onChange = vi.fn();
    render(<WorkspaceTabs active="chat" onChange={onChange} />);
    await userEvent.click(screen.getByText("Git"));
    expect(onChange).toHaveBeenCalledWith("git");
  });
});
