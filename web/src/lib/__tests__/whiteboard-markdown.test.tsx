import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WhiteboardMarkdown, toggleTaskAtIndex } from "@/lib/whiteboard-markdown";

describe("WhiteboardMarkdown", () => {
  it("渲染标题、引用和分隔线", () => {
    const { container } = render(
      <WhiteboardMarkdown text={"# 白板标题\n\n> 这里是一条引用\n\n---"} />,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "白板标题" }),
    ).toBeInTheDocument();
    expect(screen.getByText("这里是一条引用")).toBeInTheDocument();
    expect(container.querySelector("blockquote")).not.toBeNull();
    expect(container.querySelector("hr")).not.toBeNull();
  });

  it("把 task list 渲染成便签式清单项", () => {
    render(
      <WhiteboardMarkdown text={"- [ ] 未完成事项\n- [x] 已完成事项"} />,
    );

    expect(screen.getByText("未完成事项")).toBeInTheDocument();
    const doneItem = screen.getByText("已完成事项");
    expect(doneItem).toBeInTheDocument();
    expect(doneItem.className).toContain("line-through");
  });

  it("保留普通列表和行内样式", () => {
    render(
      <WhiteboardMarkdown
        text={"1. **第一项**\n2. 包含 `code` 和 [link](https://example.com)"}
      />,
    );

    expect(screen.getByText("第一项")).toBeInTheDocument();
    expect(screen.getByText("code")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "link" })).toHaveAttribute(
      "href",
      "https://example.com",
    );
  });

  it("点击 task list 会回调对应索引", () => {
    const onToggleTask = vi.fn();
    render(
      <WhiteboardMarkdown
        text={"- [ ] 第一项\n- [x] 第二项"}
        onToggleTask={onToggleTask}
      />,
    );

    fireEvent.click(screen.getByText("第一项"));
    fireEvent.click(screen.getByText("第二项"));

    expect(onToggleTask).toHaveBeenNthCalledWith(1, 0);
    expect(onToggleTask).toHaveBeenNthCalledWith(2, 1);
  });

  it("toggleTaskAtIndex 会回写 markdown 勾选状态", () => {
    const content = ["# 卡片", "", "- [ ] 第一项", "- [x] 第二项"].join("\n");

    expect(toggleTaskAtIndex(content, 0)).toContain("- [x] 第一项");
    expect(toggleTaskAtIndex(content, 1)).toContain("- [ ] 第二项");
  });
});
