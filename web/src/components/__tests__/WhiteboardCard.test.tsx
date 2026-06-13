import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WhiteboardCard, type WhiteboardCardItem } from "@/components/WhiteboardCard";

const card: WhiteboardCardItem = {
  id: "card-1",
  scope: "project",
  title: "New Card 1",
  category: "note",
  content: "# New Card",
  collapsed: false,
  height: 280,
  updatedLabel: "Synced",
};

describe("WhiteboardCard", () => {
  it("uses the card shell as drag surface and the bottom edge as resize surface", () => {
    const onDragPointerDown = vi.fn();
    const onResizePointerDown = vi.fn();

    render(
      <WhiteboardCard
        card={card}
        onDragPointerDown={onDragPointerDown}
        onResizePointerDown={onResizePointerDown}
      />,
    );

    fireEvent.pointerDown(screen.getByTestId("whiteboard-card-shell"));
    fireEvent.pointerDown(screen.getByTestId("whiteboard-card-resize-edge"));

    expect(onDragPointerDown).toHaveBeenCalledTimes(1);
    expect(onResizePointerDown).toHaveBeenCalledTimes(1);
  });

  it("opens editor when preview content is clicked and returns to preview on blur", () => {
    render(<WhiteboardCard card={card} />);

    fireEvent.click(screen.getByRole("button", { name: /new card/i }));
    const editor = screen.getByLabelText("Whiteboard card editor");
    expect(editor).toBeInTheDocument();

    fireEvent.blur(editor);
    expect(screen.queryByLabelText("Whiteboard card editor")).toBeNull();
  });

  it("starts empty cards in editor mode", () => {
    render(
      <WhiteboardCard
        card={{
          ...card,
          id: "card-empty",
          content: "",
        }}
      />,
    );

    expect(screen.getByLabelText("Whiteboard card editor")).toBeInTheDocument();
  });

  it("toggles todo items directly in preview mode", () => {
    const onUpdateCard = vi.fn();
    render(
      <WhiteboardCard
        card={{
          ...card,
          id: "card-todo",
          category: "todo",
          content: "- [ ] first task",
        }}
        onUpdateCard={onUpdateCard}
      />,
    );

    fireEvent.click(screen.getByText("first task").closest("button")!);
    expect(onUpdateCard).toHaveBeenCalledWith("card-todo", {
      content: "- [x] first task",
    });
  });

  it("renders global scope with custom border color via inline style", () => {
    const globalCard: WhiteboardCardItem = { ...card, scope: "global" };
    render(<WhiteboardCard card={globalCard} />);

    const shell = screen.getByTestId("whiteboard-card-shell");
    expect(shell).toHaveStyle({
      borderColor: "hsl(var(--whiteboard-scope-global-border))",
    });
  });

  it("project scope does not set custom border color inline", () => {
    const projectCard: WhiteboardCardItem = { ...card, scope: "project" };
    render(<WhiteboardCard card={projectCard} />);

    const shell = screen.getByTestId("whiteboard-card-shell");
    expect(shell.style.borderColor).toBe("");
  });
});
