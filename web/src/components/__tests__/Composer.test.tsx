import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  Composer,
  type ReasoningEffort,
  type SubmittedDraft,
} from "@/components/Composer";
import { getComposerTextareaHeightBounds } from "@/lib/composer-textarea";

function expectedSubmittedDraft(
  text: string,
  reasoningEffort: ReasoningEffort | null = null,
): SubmittedDraft {
  return {
    text,
    reasoningEffort,
    attachments: [],
    references: [],
  };
}

let mockScrollHeight = 0;
const originalScrollHeight = Object.getOwnPropertyDescriptor(
  HTMLTextAreaElement.prototype,
  "scrollHeight",
);
const originalGetComputedStyle = window.getComputedStyle.bind(window);

beforeEach(() => {
  mockScrollHeight = 0;
  Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
    configurable: true,
    get: () => mockScrollHeight,
  });
  vi.spyOn(window, "getComputedStyle").mockImplementation(
    (element) => {
      const style = originalGetComputedStyle(element);
      return new Proxy(style, {
        get(target, prop, receiver) {
          if (prop === "lineHeight") return "20px";
          if (prop === "paddingTop") return "8px";
          if (prop === "paddingBottom") return "8px";
          if (prop === "borderTopWidth") return "1px";
          if (prop === "borderBottomWidth") return "1px";
          return Reflect.get(target, prop, receiver);
        },
      }) as CSSStyleDeclaration;
    },
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  if (originalScrollHeight) {
    Object.defineProperty(
      HTMLTextAreaElement.prototype,
      "scrollHeight",
      originalScrollHeight,
    );
    return;
  }
  Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
    configurable: true,
    get: () => 0,
  });
});

describe("Composer", () => {
  it("⌘⏎ 提交（meta+Enter）", async () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const user = userEvent.setup();
    await user.type(
      screen.getByLabelText("消息输入"),
      "hi{Meta>}{Enter}{/Meta}",
    );
    expect(onSubmit).toHaveBeenCalledWith(
      "hi",
      null,
      undefined,
      undefined,
      expectedSubmittedDraft("hi"),
    );
  });

  it("点击发送按钮提交", async () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("消息输入"), "hello");
    await user.click(screen.getByRole("button", { name: /发送/ }));
    expect(onSubmit).toHaveBeenCalledWith(
      "hello",
      null,
      undefined,
      undefined,
      expectedSubmittedDraft("hello"),
    );
  });

  it("onSubmit 返回 false 时保留输入内容", async () => {
    const onSubmit = vi.fn().mockResolvedValue(false);
    render(<Composer onSubmit={onSubmit} />);
    const user = userEvent.setup();
    const input = screen.getByLabelText("消息输入");

    await user.type(input, "keep me");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(onSubmit).toHaveBeenCalledWith(
      "keep me",
      null,
      undefined,
      undefined,
      expectedSubmittedDraft("keep me"),
    );
    expect(input).toHaveValue("keep me");
  });

  it("restoreDraftToken 变化时恢复最近一次已提交草稿", async () => {
    const onSubmit = vi.fn();
    const { rerender } = render(
      <Composer onSubmit={onSubmit} restoreDraftToken={null} />,
    );
    const user = userEvent.setup();
    const input = screen.getByLabelText("消息输入");

    await user.type(input, "restore me");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(input).toHaveValue("");

    rerender(<Composer onSubmit={onSubmit} restoreDraftToken={1} />);

    expect(input).toHaveValue("restore me");
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

  it("模型没有 reasoning capability 时隐藏深度思考控件", () => {
    render(<Composer onSubmit={vi.fn()} reasoningOptions={[]} />);
    expect(screen.queryByRole("button", { name: /深度思考/ })).toBeNull();
  });

  it("模型切换后采用 catalog 默认 effort 并随发送提交", async () => {
    const onSubmit = vi.fn();
    const { rerender } = render(
      <Composer
        onSubmit={onSubmit}
        reasoningOptions={["none", "high"]}
        defaultReasoningEffort="high"
      />,
    );
    expect(screen.getByRole("button", { name: /深度思考/ })).toHaveTextContent("高");

    rerender(
      <Composer
        onSubmit={onSubmit}
        reasoningOptions={["none", "low"]}
        defaultReasoningEffort="low"
      />,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("消息输入"), "use catalog default");
    await user.click(screen.getByRole("button", { name: /发送/ }));
    expect(onSubmit).toHaveBeenCalledWith(
      "use catalog default",
      "low",
      undefined,
      undefined,
      expectedSubmittedDraft("use catalog default", "low"),
    );
  });

  it("同一模型 capability 刷新后保留用户显式选择", async () => {
    const onSubmit = vi.fn();
    const { rerender } = render(
      <Composer
        onSubmit={onSubmit}
        reasoningOptions={["none", "high"]}
        defaultReasoningEffort="high"
      />,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /深度思考/ }));
    await user.click(screen.getByRole("menuitemradio", { name: "关闭" }));

    rerender(
      <Composer
        onSubmit={onSubmit}
        reasoningOptions={["none", "high"]}
        defaultReasoningEffort="high"
      />,
    );
    await user.type(screen.getByLabelText("消息输入"), "keep explicit none");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(onSubmit).toHaveBeenCalledWith(
      "keep explicit none",
      "none",
      undefined,
      undefined,
      expectedSubmittedDraft("keep explicit none", "none"),
    );
  });

  it("成功发送后保留用户显式选择", async () => {
    const onSubmit = vi.fn();
    render(
      <Composer
        onSubmit={onSubmit}
        reasoningOptions={["none", "high", "max"]}
        defaultReasoningEffort="high"
      />,
    );
    const user = userEvent.setup();
    const input = screen.getByLabelText("消息输入");

    await user.click(screen.getByRole("button", { name: /深度思考/ }));
    await user.click(screen.getByRole("menuitemradio", { name: "最高" }));
    await user.type(input, "first");
    await user.click(screen.getByRole("button", { name: /发送/ }));
    await user.type(input, "second");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(onSubmit).toHaveBeenNthCalledWith(
      1,
      "first",
      "max",
      undefined,
      undefined,
      expectedSubmittedDraft("first", "max"),
    );
    expect(onSubmit).toHaveBeenNthCalledWith(
      2,
      "second",
      "max",
      undefined,
      undefined,
      expectedSubmittedDraft("second", "max"),
    );
  });

  it("组件重挂载时恢复稳定 owner 保存的 none", async () => {
    const onSubmit = vi.fn();
    render(
      <Composer
        onSubmit={onSubmit}
        reasoningOptions={["none", "high"]}
        defaultReasoningEffort="high"
        reasoningSelectionKey="preset-a"
        initialReasoningEffort="none"
      />,
    );
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("消息输入"), "restored none");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(onSubmit).toHaveBeenCalledWith(
      "restored none",
      "none",
      undefined,
      undefined,
      expectedSubmittedDraft("restored none", "none"),
    );
  });

  it("模型 identity 改变时采用新模型默认档位", async () => {
    const onSubmit = vi.fn();
    const { rerender } = render(
      <Composer
        onSubmit={onSubmit}
        reasoningOptions={["none", "high"]}
        defaultReasoningEffort="high"
        reasoningSelectionKey="preset-a"
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /深度思考/ }));
    await user.click(screen.getByRole("menuitemradio", { name: "关闭" }));

    rerender(
      <Composer
        onSubmit={onSubmit}
        reasoningOptions={["none", "high"]}
        defaultReasoningEffort="high"
        reasoningSelectionKey="preset-b"
      />,
    );
    await user.type(screen.getByLabelText("消息输入"), "new model default");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(onSubmit).toHaveBeenCalledWith(
      "new model default",
      "high",
      undefined,
      undefined,
      expectedSubmittedDraft("new model default", "high"),
    );
  });

  it("applies Composer textarea spacing and theme classes", () => {
    render(<Composer onSubmit={vi.fn()} />);
    const textarea = screen.getByRole("textbox");

    expect(textarea).toHaveClass("py-0.5");
    expect(textarea).toHaveClass("border-input/85");
    expect(textarea).toHaveClass("bg-card/82");
    expect(textarea).toHaveClass("text-card-foreground");
    expect(textarea).toHaveClass("placeholder:text-muted-foreground/80");
    expect(textarea).toHaveClass("focus-visible:border-primary/45");
    expect(textarea).toHaveClass("focus-visible:ring-primary/35");
    expect(textarea).toHaveClass("dark:border-border/90");
    expect(textarea).toHaveClass("dark:bg-background/72");
    expect(textarea).toHaveClass("dark:text-foreground");
    expect(textarea).toHaveClass("dark:placeholder:text-muted-foreground/72");
    expect(textarea).toHaveClass("dark:focus-visible:border-primary/55");
    expect(textarea).toHaveClass("dark:focus-visible:ring-primary/45");
  });

  it("点击加号后显示图片菜单项", async () => {
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();

    expect(screen.queryByTestId("composer-attach-button")).toBeNull();
    await user.click(screen.getByTestId("composer-plus-trigger"));

    expect(screen.getByTestId("composer-attach-button")).toBeInTheDocument();
    expect(screen.getByText("图片")).toBeInTheDocument();
  });

  it("底部主操作区保留加号和模型选择并允许换行", () => {
    render(
      <Composer
        onSubmit={vi.fn()}
        threadId="thread-1"
        modelSwitcher={<button type="button">选择模型</button>}
        leftActions={<button type="button">项目</button>}
      />,
    );

    const primaryActions = screen.getByTestId("composer-primary-actions");
    expect(primaryActions).toHaveClass("flex-wrap");
    expect(primaryActions).toHaveClass("flex-1");
    expect(primaryActions).toContainElement(screen.getByTestId("composer-plus-trigger"));
    expect(primaryActions).toHaveTextContent("选择模型");
    expect(screen.getByTestId("composer-secondary-actions")).toHaveClass("shrink-0");
  });

  // interrupt-run-v0.1
  it("disabled + isRunning + onInterrupt → 显示 Stop 按钮代替 Send", () => {
    const onInterrupt = vi.fn();
    render(
      <Composer
        onSubmit={vi.fn()}
        disabled={true}
        isRunning={true}
        onInterrupt={onInterrupt}
      />,
    );
    // Send 按钮被替换
    expect(screen.queryByTestId("composer-send")).toBeNull();
    expect(screen.getByTestId("composer-stop")).toBeInTheDocument();
  });

  it("点 Stop 按钮 → 触发 onInterrupt", async () => {
    const onInterrupt = vi.fn();
    render(
      <Composer
        onSubmit={vi.fn()}
        disabled={true}
        isRunning={true}
        onInterrupt={onInterrupt}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("composer-stop"));
    expect(onInterrupt).toHaveBeenCalledTimes(1);
  });

  it("disabled 但 isRunning=false → 仍显示 Send（不是 Stop）", () => {
    render(
      <Composer
        onSubmit={vi.fn()}
        disabled={true}
        isRunning={false}
        onInterrupt={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("composer-stop")).toBeNull();
    expect(screen.getByTestId("composer-send")).toBeInTheDocument();
  });

  it("isRunning=true 但 onInterrupt 未提供 → 退化为 Send 按钮（disabled）", () => {
    render(
      <Composer onSubmit={vi.fn()} disabled={true} isRunning={true} />,
    );
    expect(screen.queryByTestId("composer-stop")).toBeNull();
    expect(screen.getByTestId("composer-send")).toBeDisabled();
  });

  it("allowSubmitWhileRunning=true → 运行中同时显示 Stop 和排队发送", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <Composer
        onSubmit={onSubmit}
        threadId="thread-1"
        isRunning={true}
        allowSubmitWhileRunning={true}
        onInterrupt={vi.fn()}
      />,
    );

    expect(screen.getByTestId("composer-stop")).toBeInTheDocument();
    const send = screen.getByTestId("composer-send");
    expect(send).toHaveTextContent("排队");
    await user.type(screen.getByLabelText("消息输入"), "queued message");
    await user.click(send);
    expect(onSubmit).toHaveBeenCalledWith(
      "queued message",
      null,
      undefined,
      undefined,
      expectedSubmittedDraft("queued message"),
    );
  });

  it("keeps two lines by default and caps growth at six lines", () => {
    const bounds = getComposerTextareaHeightBounds({
      lineHeight: "20px",
      paddingTop: "8px",
      paddingBottom: "8px",
      borderTopWidth: "1px",
      borderBottomWidth: "1px",
    } as CSSStyleDeclaration);

    mockScrollHeight = bounds.minHeight;
    render(<Composer onSubmit={vi.fn()} />);
    const textarea = screen.getByLabelText("消息输入") as HTMLTextAreaElement;

    expect(textarea.rows).toBe(2);
    expect(textarea.style.height).toBe(`${bounds.minHeight}px`);
    expect(textarea.style.overflowY).toBe("hidden");

    mockScrollHeight = bounds.minHeight + 20;
    fireEvent.change(textarea, { target: { value: "a\nb\nc" } });
    expect(textarea.style.height).toBe(`${bounds.minHeight + 20}px`);
    expect(textarea.style.overflowY).toBe("hidden");

    mockScrollHeight = bounds.maxHeight + 24;
    fireEvent.change(textarea, {
      target: { value: "1\n2\n3\n4\n5\n6\n7" },
    });
    expect(textarea.style.height).toBe(`${bounds.maxHeight}px`);
    expect(textarea.style.overflowY).toBe("auto");
  });
});
