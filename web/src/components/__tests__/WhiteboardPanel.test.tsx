import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WhiteboardPanel } from "@/components/WhiteboardPanel";

describe("WhiteboardPanel", () => {
  it("renders whiteboard content in embedded dock mode", () => {
    render(
      <WhiteboardPanel
        title="Dock Whiteboard"
        cards={[]}
        isOpen={false}
        embedded={true}
      />,
    );

    expect(screen.getByText("Dock Whiteboard")).toBeInTheDocument();
    expect(screen.getByTestId("whiteboard-panel").className).toContain("flex-1");
  });

  it("omits standalone resize and edge handles in embedded dock mode", () => {
    render(
      <WhiteboardPanel
        title="Dock Whiteboard"
        cards={[]}
        isOpen={false}
        embedded={true}
      />,
    );

    expect(screen.queryByTestId("whiteboard-resize-handle")).toBeNull();
    expect(screen.queryByTestId("whiteboard-edge-handle")).toBeNull();
    expect(screen.queryByRole("button", { name: "隐藏白板" })).toBeNull();
    expect(screen.queryByRole("button", { name: "展开白板" })).toBeNull();
  });

  it("creates cards from embedded dock mode", () => {
    const onCreateCard = vi.fn();
    render(
      <WhiteboardPanel
        title="Dock Whiteboard"
        cards={[]}
        isOpen={false}
        variant="dock"
        onCreateCard={onCreateCard}
      />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: "新建便签" })[0]!);
    expect(onCreateCard).toHaveBeenCalledWith("project", "note");
  });

  it("shows the collapse button while open", () => {
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

  it("shows the edge handle while closed", () => {
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

  it("creates note and todo cards from the empty state", () => {
    const onCreateCard = vi.fn();
    render(
      <WhiteboardPanel
        title="白板"
        cards={[]}
        isOpen={true}
        onCreateCard={onCreateCard}
      />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: "新建便签" })[0]!);
    expect(onCreateCard).toHaveBeenCalledWith("project", "note");

    fireEvent.click(screen.getAllByRole("button", { name: "新建待办" })[0]!);
    expect(onCreateCard).toHaveBeenCalledWith("project", "todo");
  });

  it("creates global note and todo cards when no project board is bound", () => {
    const onCreateCard = vi.fn();
    render(
      <WhiteboardPanel
        title="Whiteboard"
        projectTitle={null}
        cards={[]}
        isOpen={true}
        onCreateCard={onCreateCard}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "新建便签" }));
    expect(onCreateCard).toHaveBeenCalledWith("global", "note");

    fireEvent.click(screen.getByRole("button", { name: "新建待办" }));
    expect(onCreateCard).toHaveBeenCalledWith("global", "todo");
  });

  it("uses the edge handle shape on mobile while closed", () => {
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
