import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WhiteboardPanel } from "@/components/WhiteboardPanel";

describe("WhiteboardPanel", () => {
  it("展开态显示收起按钮并触发回调", () => {
    const onToggleOpen = vi.fn();
    render(
      <WhiteboardPanel
        title="白板"
        cards={[]}
        isOpen={true}
        onToggleOpen={onToggleOpen}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "隐藏白板" }));
    expect(onToggleOpen).toHaveBeenCalledTimes(1);
  });

  it("收起态显示展开把手并触发回调", () => {
    const onToggleOpen = vi.fn();
    render(
      <WhiteboardPanel
        title="白板"
        cards={[]}
        isOpen={false}
        onToggleOpen={onToggleOpen}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "展开白板" }));
    expect(onToggleOpen).toHaveBeenCalledTimes(1);
  });

  it("手机收起态使用边缘微把手且宽度归零", () => {
    const onToggleOpen = vi.fn();
    const { container } = render(
      <WhiteboardPanel
        title="白板"
        cards={[]}
        isOpen={false}
        compactMode={true}
        mobileMode={true}
        onToggleOpen={onToggleOpen}
      />,
    );

    fireEvent.click(screen.getByTestId("whiteboard-edge-handle"));
    expect(onToggleOpen).toHaveBeenCalledTimes(1);
    expect(container.querySelector("aside")?.className).toContain("w-0");
  });
});
