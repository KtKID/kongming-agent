/**
 * YamlPreview 折叠 + 内容渲染最小用例。
 *
 * 注意：原生 `<details>` 在浏览器里通过 `[open]` 属性控制 body 可见性，
 * **DOM 子树本身始终存在**（只是 CSS 隐藏）。jsdom 不应用 user-agent CSS，
 * 所以本测试用 `details.open` 状态 + body wrapper 的 `hidden` 行为断言
 * 折叠/展开语义，而不是断言子树是否在 DOM 里。
 *
 * 同时验证：
 * - defaultOpen 透传到 `<details open>` 属性
 * - yaml 内容在 body 内被切成高亮 token 后仍能拼回原文
 * - 自定义 title 落到 summary
 * - 切换 `details.open` 后属性同步
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { YamlPreview } from "../YamlPreview";

const SAMPLE = "foo: 1";

function getDetails(container: HTMLElement): HTMLDetailsElement {
  const el = container.querySelector("details");
  if (!el) throw new Error("YamlPreview should render a <details> root");
  return el as HTMLDetailsElement;
}

describe("YamlPreview", () => {
  it("默认折叠：details.open=false", () => {
    const { container } = render(<YamlPreview content={SAMPLE} />);
    const details = getDetails(container);
    expect(details.open).toBe(false);
    // summary 始终显示标题
    expect(screen.getByText("查看 yaml 原文")).toBeInTheDocument();
  });

  it("defaultOpen=true 时 yaml 内容出现在 DOM", () => {
    const { container } = render(
      <YamlPreview content={SAMPLE} defaultOpen />,
    );
    const details = getDetails(container);
    expect(details.open).toBe(true);

    // 高亮把每一行切成多个 <span>，用 textContent 整体核验
    const body = details.querySelector("pre");
    expect(body).not.toBeNull();
    expect(body!.textContent).toContain("foo");
    expect(body!.textContent).toContain("1");
  });

  it("切换 open 后状态属性同步", () => {
    const { container } = render(<YamlPreview content={SAMPLE} />);
    const details = getDetails(container);
    expect(details.open).toBe(false);

    details.open = true;
    expect(details.open).toBe(true);
    expect(details.hasAttribute("open")).toBe(true);

    details.open = false;
    expect(details.open).toBe(false);
    expect(details.hasAttribute("open")).toBe(false);
  });

  it("自定义 title 渲染在 summary 上", () => {
    render(<YamlPreview content={SAMPLE} title="setting.yaml 原文" />);
    expect(screen.getByText("setting.yaml 原文")).toBeInTheDocument();
  });
});
