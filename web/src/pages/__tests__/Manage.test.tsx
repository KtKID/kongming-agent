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
            <Route path="plugins" element={<div>Plugins Child</div>} />
            <Route path="model-providers" element={<div>Providers Child</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("运行管理")).toBeInTheDocument();

    const configTab = screen.getByRole("tab", { name: "配置" });
    const pluginsTab = screen.getByRole("tab", { name: "插件" });
    const providersTab = screen.getByRole("tab", { name: "模型服务商" });
    const networkTab = screen.getByRole("tab", { name: "网络" });
    expect(configTab).toHaveAttribute("href", "/manage/config");
    expect(pluginsTab).toHaveAttribute("href", "/manage/plugins");
    expect(providersTab).toHaveAttribute("href", "/manage/model-providers");
    expect(networkTab).toHaveAttribute("href", "/manage/network");

    expect(screen.getByText("Runtime Child")).toBeInTheDocument();
  });

  it("/manage/plugins 渲染插件子页面", () => {
    render(
      <MemoryRouter initialEntries={["/manage/plugins"]}>
        <Routes>
          <Route path="/manage" element={<ManagePage />}>
            <Route path="plugins" element={<div>Plugins Child</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("tab", { name: "插件" })).toHaveAttribute(
      "href",
      "/manage/plugins",
    );
    expect(screen.getByText("Plugins Child")).toBeInTheDocument();
  });

  it("/manage/model-providers 渲染模型服务商子页面", () => {
    render(
      <MemoryRouter initialEntries={["/manage/model-providers"]}>
        <Routes>
          <Route path="/manage" element={<ManagePage />}>
            <Route path="model-providers" element={<div>Providers Child</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("tab", { name: "模型服务商" })).toHaveAttribute(
      "href",
      "/manage/model-providers",
    );
    expect(screen.getByText("Providers Child")).toBeInTheDocument();
  });
});
