import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PhaseIndicator } from "@/components/PhaseIndicator";

describe("PhaseIndicator 终态展示", () => {
  it("complete 显示完成标记", () => {
    render(<PhaseIndicator phase="complete" />);

    expect(screen.getByLabelText("完成")).toHaveTextContent("✓");
  });

  it("error 显示错误标记", () => {
    render(<PhaseIndicator phase="error" />);

    expect(screen.getByLabelText("错误")).toHaveTextContent("✗");
  });

  it("idle 不显示状态标记", () => {
    const { container } = render(<PhaseIndicator phase="idle" />);

    expect(container).toBeEmptyDOMElement();
  });
});
