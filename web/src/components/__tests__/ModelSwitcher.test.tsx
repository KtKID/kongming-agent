import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ModelSwitcher } from "@/components/ModelSwitcher";
import type { ConnectedModelFamily } from "@/modules/model-providers/types";

const families: ConnectedModelFamily[] = [
  {
    providerId: "minimax",
    providerLabel: "Minimax（CN）",
    familyId: "minimax:MiniMax-M3",
    displayName: "MiniMax-M3",
    presetId: "minimax-cn",
    model: "MiniMax-M3",
    connected: true,
    supportedReasoningEfforts: ["none", "high"],
    defaultReasoningEffort: "high",
    reasoningAdapter: "anthropic_thinking_toggle",
    contextWindowTokens: 200000,
  },
  {
    providerId: "glm",
    providerLabel: "GLM（CN）",
    familyId: "glm:glm-5.1",
    displayName: "glm-5.1",
    presetId: "bigmodel-glm5",
    model: "glm-5.1",
    connected: true,
    supportedReasoningEfforts: ["none", "high", "max"],
    defaultReasoningEffort: "high",
    reasoningAdapter: "glm_thinking_toggle",
    contextWindowTokens: 1000000,
  },
];

describe("ModelSwitcher", () => {
  it("展示当前模型并把模型服务商放在菜单顶部", async () => {
    const user = userEvent.setup();
    const onManageProviders = vi.fn();

    render(
      <MemoryRouter initialEntries={["/chat/thread-aaaaaaaaaaaa"]}>
        <ModelSwitcher
          currentPresetId="minimax-cn"
          options={families}
          onSelect={vi.fn()}
          onManageProviders={onManageProviders}
        />
        <Routes>
          <Route path="/chat/:threadId" element={null} />
          <Route
            path="/manage/model-providers"
            element={<div data-testid="manage-model-providers-route" />}
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByTestId("composer-model-switcher")).toHaveTextContent(
      "MiniMax-M3",
    );

    await user.click(screen.getByTestId("composer-model-switcher"));
    const menu = screen.getByTestId("composer-model-menu");
    const items = within(menu).getAllByRole("menuitem");
    expect(items[0]).toHaveTextContent("模型服务商");
    expect(items[1]).toHaveTextContent("MiniMax-M3");
    expect(items[1]).toHaveTextContent("关闭 / 高");
    expect(items[2]).toHaveTextContent("glm-5.1");
    expect(items[2]).toHaveTextContent("关闭 / 高 / 最高");

    await user.click(screen.getByTestId("composer-model-manage"));
    expect(onManageProviders).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("manage-model-providers-route")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByTestId("composer-model-menu")).not.toBeInTheDocument(),
    );
  });

  it("选择模型时传出目标 preset id，并显示当前选中 check", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();

    render(
      <MemoryRouter>
        <ModelSwitcher
          currentPresetId="bigmodel-glm5"
          options={families}
          onSelect={onSelect}
        />
      </MemoryRouter>,
    );

    await user.click(screen.getByTestId("composer-model-switcher"));
    expect(screen.getByTestId("composer-model-check-glm")).toHaveClass("opacity-100");
    expect(screen.getByTestId("composer-model-check-minimax")).toHaveClass(
      "opacity-0",
    );

    await user.click(screen.getByTestId("composer-model-option-minimax"));
    expect(onSelect).toHaveBeenCalledWith("minimax-cn");
  });

  it("没有已连接模型时展示空状态", async () => {
    const user = userEvent.setup();

    render(
      <MemoryRouter>
        <ModelSwitcher currentPresetId={null} options={[]} onSelect={vi.fn()} />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("composer-model-switcher")).toHaveTextContent(
      "选择模型",
    );
    await user.click(screen.getByTestId("composer-model-switcher"));
    expect(screen.getByText("暂无已连接模型")).toBeInTheDocument();
  });

  it("当前 preset 不在已连接列表时不伪装成第一个候选模型", () => {
    render(
      <MemoryRouter>
        <ModelSwitcher
          currentPresetId="missing-preset"
          options={families}
          onSelect={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("composer-model-switcher")).toHaveTextContent(
      "当前模型未连接",
    );
  });
});
