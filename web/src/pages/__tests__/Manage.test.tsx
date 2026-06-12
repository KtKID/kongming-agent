import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect } from "vitest";
import { ManagePage } from "@/pages/Manage";

describe("ManagePage", () => {
  it("渲染管理壳、左侧竖 tab 与子页面内容", () => {
    render(
      <MemoryRouter initialEntries={["/manage/network"]}>
        <Routes>
          <Route path="/manage" element={<ManagePage />}>
            <Route path="network" element={<div>Runtime Child</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("运行管理")).toBeInTheDocument();

    const configTab = screen.getByRole("tab", { name: "配置" });
    const networkTab = screen.getByRole("tab", { name: "网络" });
    expect(configTab).toHaveAttribute("href", "/manage/config");
    expect(networkTab).toHaveAttribute("href", "/manage/network");

    expect(screen.getByText("Runtime Child")).toBeInTheDocument();
  });
});
