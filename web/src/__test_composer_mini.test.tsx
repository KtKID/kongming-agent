import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Composer } from "@/components/Composer";

describe("Composer mini", () => {
  it("disabled=true → textarea disabled", () => {
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} disabled={true} />);
    expect(screen.getByLabelText("消息输入")).toBeDisabled();
  });
});
