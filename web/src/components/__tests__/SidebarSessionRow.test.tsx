import { render, screen } from "@testing-library/react";
import { MessageSquare } from "lucide-react";
import { describe, expect, it, vi } from "vitest";
import { SidebarSessionRow } from "@/components/SidebarSessionRow";

describe("SidebarSessionRow", () => {
  it("默认态接近矩形且不显示明显背景边界", () => {
    render(<SidebarSessionRow title="alpha" onOpen={vi.fn()} />);

    const row = screen.getByRole("button", { name: "alpha" });
    expect(row).toHaveClass("rounded-lg");
    expect(row).toHaveClass("border-transparent");
    expect(row).toHaveClass("bg-transparent");
    expect(row).toHaveClass("shadow-none");
    expect(row).toHaveClass("hover:border-border/70");
    expect(row).toHaveClass("hover:bg-card/80");
    expect(row).toHaveClass("hover:shadow-sm");
  });

  it("选中态显示边界和背景", () => {
    render(<SidebarSessionRow title="active" selected onOpen={vi.fn()} />);

    const row = screen.getByRole("button", { name: "active" });
    expect(row).toHaveClass("rounded-lg");
    expect(row).toHaveClass("border-primary/22");
    expect(row).toHaveClass("bg-primary/10");
    expect(row).toHaveClass("shadow-sm");
  });

  it("编辑态使用同样的小圆角", () => {
    render(
      <SidebarSessionRow
        title="editing"
        editing
        editSlot={<input aria-label="编辑标题" />}
        leading={<MessageSquare />}
        onOpen={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("编辑标题").closest("div")?.parentElement).toHaveClass(
      "rounded-lg",
    );
  });
});
