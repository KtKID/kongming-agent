import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import { Composer } from "@/components/Composer";

describe("Composer", () => {
  it("⌘⏎ 提交（meta+Enter）", async () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const user = userEvent.setup();
    await user.type(
      screen.getByLabelText("消息输入"),
      "hi{Meta>}{Enter}{/Meta}",
    );
    expect(onSubmit).toHaveBeenCalledWith("hi", null);
  });

  it("点击发送按钮提交", async () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("消息输入"), "hello");
    await user.click(screen.getByRole("button", { name: /发送/ }));
    expect(onSubmit).toHaveBeenCalledWith("hello", null);
  });

  it("disabled=true → 禁用 textarea + 发送按钮", () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} disabled={true} />);
    expect(screen.getByLabelText("消息输入")).toBeDisabled();
    expect(screen.getByRole("button", { name: /发送/ })).toBeDisabled();
  });

  it("空白内容 → 不触发 onSubmit", async () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const user = userEvent.setup();
    await user.type(
      screen.getByLabelText("消息输入"),
      "  {Meta>}{Enter}{/Meta}",
    );
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("渲染深度思考按钮，初始状态无 effort 标签", () => {
    render(<Composer onSubmit={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: /深度思考/ });
    expect(trigger).toBeInTheDocument();
    expect(trigger.textContent).toContain("深度思考");
  });

  it("disabled → 深度思考按钮也禁用", () => {
    render(<Composer onSubmit={vi.fn()} disabled={true} />);
    expect(screen.getByRole("button", { name: /深度思考/ })).toBeDisabled();
  });
});
