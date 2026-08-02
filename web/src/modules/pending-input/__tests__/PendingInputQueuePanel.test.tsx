import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PendingInputQueuePanel } from "../PendingInputQueuePanel";
import type { PendingInputDTO } from "@/protocol";

function item(id: string, content: string, sequence: number): PendingInputDTO {
  return {
    id,
    thread_id: "thread-aaaaaaaaaaaa",
    source: "user_input",
    priority: "user_message",
    content,
    preview: content,
    status: "queued",
    created_at_ms: sequence,
    updated_at_ms: sequence,
    sequence,
    metadata: {},
  };
}

function mockItemRects(rows: HTMLElement[]) {
  rows.forEach((row, index) => {
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: index * 50,
      top: index * 50,
      bottom: index * 50 + 40,
      left: 0,
      right: 320,
      width: 320,
      height: 40,
      toJSON: () => ({}),
    } as DOMRect);
  });
}

describe("PendingInputQueuePanel", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders queued inputs and supports edit and delete", async () => {
    const user = userEvent.setup();
    const onUpdate = vi.fn();
    const onCancel = vi.fn();
    const onSendNow = vi.fn();
    const onReorder = vi.fn();
    render(
      <PendingInputQueuePanel
        items={[item("pin-1", "first", 1), item("pin-2", "second", 2)]}
        maxItems={20}
        onUpdate={onUpdate}
        onCancel={onCancel}
        onSendNow={onSendNow}
        onReorder={onReorder}
      />,
    );

    expect(screen.getByTestId("pending-input-queue")).toHaveTextContent("2/20");
    expect(screen.getAllByTestId("pending-input-item")).toHaveLength(2);
    expect(screen.getAllByTestId("pending-input-drag-handle")).toHaveLength(2);
    expect(screen.queryByLabelText("上移待发送消息")).toBeNull();
    expect(screen.queryByLabelText("下移待发送消息")).toBeNull();

    await user.click(screen.getAllByLabelText("编辑待发送消息")[0]);
    await user.clear(screen.getByTestId("pending-input-edit"));
    await user.type(screen.getByTestId("pending-input-edit"), "updated");
    await user.click(screen.getByLabelText("保存待发送消息"));
    expect(onUpdate).toHaveBeenCalledWith("pin-1", "updated");

    await user.click(screen.getAllByLabelText("删除待发送消息")[1]);
    expect(onCancel).toHaveBeenCalledWith("pin-2");
    await user.click(screen.getAllByLabelText("立即发送待发送消息")[0]);
    expect(onSendNow).toHaveBeenCalledWith("pin-1");
    expect(onReorder).not.toHaveBeenCalled();
  });

  it("submits final ordered ids after drag release", () => {
    const onReorder = vi.fn();
    render(
      <PendingInputQueuePanel
        items={[
          item("pin-1", "first", 1),
          item("pin-2", "second", 2),
          item("pin-3", "third", 3),
          item("pin-4", "fourth", 4),
        ]}
        maxItems={20}
        onUpdate={vi.fn()}
        onCancel={vi.fn()}
        onSendNow={vi.fn()}
        onReorder={onReorder}
      />,
    );

    mockItemRects(screen.getAllByTestId("pending-input-item"));
    const handles = screen.getAllByTestId("pending-input-drag-handle");

    fireEvent.pointerDown(handles[3], {
      pointerId: 1,
      clientY: 175,
      buttons: 1,
    });
    fireEvent.pointerMove(handles[3], {
      pointerId: 1,
      clientY: 65,
      buttons: 1,
    });
    expect(onReorder).not.toHaveBeenCalled();

    fireEvent.pointerUp(handles[3], {
      pointerId: 1,
      clientY: 65,
    });

    expect(onReorder).toHaveBeenCalledWith(["pin-1", "pin-4", "pin-2", "pin-3"]);
  });

  it("prunes locally dragged item when server queue starts it before release", () => {
    const onReorder = vi.fn();
    const { rerender } = render(
      <PendingInputQueuePanel
        items={[item("pin-1", "333", 1), item("pin-2", "222", 2)]}
        maxItems={20}
        onUpdate={vi.fn()}
        onCancel={vi.fn()}
        onSendNow={vi.fn()}
        onReorder={onReorder}
      />,
    );

    const staleHandle = screen.getAllByTestId("pending-input-drag-handle")[0];
    fireEvent.pointerDown(staleHandle, {
      pointerId: 1,
      clientY: 25,
      buttons: 1,
    });

    rerender(
      <PendingInputQueuePanel
        items={[item("pin-2", "222", 2)]}
        maxItems={20}
        onUpdate={vi.fn()}
        onCancel={vi.fn()}
        onSendNow={vi.fn()}
        onReorder={onReorder}
      />,
    );

    expect(screen.queryByText("333")).toBeNull();
    expect(screen.getByText("222")).toBeInTheDocument();

    fireEvent.pointerUp(staleHandle, {
      pointerId: 1,
      clientY: 25,
    });

    expect(onReorder).not.toHaveBeenCalled();
  });

  it("shows queue full error text", () => {
    render(
      <PendingInputQueuePanel
        items={[item("pin-1", "first", 1)]}
        maxItems={20}
        error="队列已满"
        onUpdate={vi.fn()}
        onCancel={vi.fn()}
        onSendNow={vi.fn()}
        onReorder={vi.fn()}
      />,
    );

    expect(screen.getByTestId("pending-input-error")).toHaveTextContent("队列已满");
  });
});
