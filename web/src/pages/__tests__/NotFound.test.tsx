import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect } from "vitest";
import { NotFoundPage } from "@/pages/NotFound";

describe("NotFoundPage", () => {
  it("显示 404 + 返回链接", () => {
    render(
      <MemoryRouter>
        <NotFoundPage />
      </MemoryRouter>,
    );
    expect(screen.getByText("404")).toBeInTheDocument();
    expect(screen.getByText("页面不存在")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /返回对话/ });
    expect(link).toHaveAttribute("href", "/chat");
  });
});
