import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { WorkspaceTabs } from "@/components/WorkspaceTabs";

describe("WorkspaceTabs", () => {
  it("renders Chat / Files / Git / Shell tabs", () => {
    render(<WorkspaceTabs active="chat" onChange={vi.fn()} />);
    expect(screen.getByText("Chat")).toBeInTheDocument();
    expect(screen.getByText("Files")).toBeInTheDocument();
    expect(screen.getByText("Git")).toBeInTheDocument();
    expect(screen.getByText("Shell")).toBeInTheDocument();
  });

  it("calls onChange('git') when Git is clicked", async () => {
    const onChange = vi.fn();
    render(<WorkspaceTabs active="chat" onChange={onChange} />);
    await userEvent.click(screen.getByText("Git"));
    expect(onChange).toHaveBeenCalledWith("git");
  });

  it("renders the thread id toolbar when thread id exists", () => {
    render(<WorkspaceTabs active="chat" onChange={vi.fn()} threadId="thread-123" />);
    expect(screen.getByText("thread-123")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy ID" })).toBeInTheDocument();
  });
});
