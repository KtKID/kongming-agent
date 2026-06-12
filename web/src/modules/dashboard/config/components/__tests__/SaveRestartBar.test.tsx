/**
 * SaveRestartBar 行为最小用例。
 *
 * 状态来源：直接用真实 `useConfigStore` + `setState` 注入；每个用例
 * `beforeEach` 调 `reset()` 清干净，避免相互污染。
 *
 * window.location.reload 不能让 jsdom 真跳——用 spy 替换并断言被调用 1 次。
 * 用 fake timers 推进 `ready → reload` 之间 500ms 的延迟。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";

import { SaveRestartBar } from "../SaveRestartBar";
import { useConfigStore } from "../../store";

// reload 在 jsdom 下默认是 noop，但显式 spy 更安全，能断言"调用了几次"
let reloadSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  // 把全部状态归零，避免上一个用例的 pendingRestartFields / saveStatus 污染
  useConfigStore.getState().reset();

  reloadSpy = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload: reloadSpy },
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("SaveRestartBar", () => {
  it("dirtyCount=0：保存按钮 disabled", () => {
    render(<SaveRestartBar />);
    const btn = screen.getByTestId("save-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("dirtyCount=3：保存按钮 enabled，文案含 3", () => {
    useConfigStore.setState({ dirtyCount: 3 });
    render(<SaveRestartBar />);
    const btn = screen.getByTestId("save-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toContain("3");
  });

  it("saveStatus=saving：保存按钮 disabled，文案含 '保存中'", () => {
    useConfigStore.setState({ dirtyCount: 2, saveStatus: "saving" });
    render(<SaveRestartBar />);
    const btn = screen.getByTestId("save-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toContain("保存中");
  });

  it("validation_error + 4 条错误：显示前 3 条 + '+1 more'", () => {
    useConfigStore.setState({
      dirtyCount: 4,
      saveStatus: "validation_error",
      validationErrors: [
        { path: "model.temperature", message: "应为 0-2 之间的数字" },
        { path: "model.top_p", message: "应为 0-1 之间的数字" },
        { path: "runtime.max_turns", message: "应为正整数" },
        { path: "stream.buffer_size", message: "应为正整数" },
      ],
    });
    render(<SaveRestartBar />);
    const list = screen.getByTestId("validation-error-list");
    const items = within(list).getAllByRole("listitem");
    // 3 条 + 1 条 "+N more" = 4 个 <li>
    expect(items).toHaveLength(4);
    expect(within(list).getByText(/model\.temperature/)).toBeInTheDocument();
    expect(within(list).getByText(/model\.top_p/)).toBeInTheDocument();
    expect(within(list).getByText(/runtime\.max_turns/)).toBeInTheDocument();
    expect(within(list).queryByText(/stream\.buffer_size/)).toBeNull();
    expect(within(list).getByText("+1 more")).toBeInTheDocument();

    // validation_error 下保存按钮仍 enabled，让用户修了再点
    const btn = screen.getByTestId("save-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it("pendingRestartFields 非空：重启按钮 enabled，文案显示项数", () => {
    useConfigStore.setState({
      pendingRestartFields: ["model.temperature"],
    });
    render(<SaveRestartBar />);
    const btn = screen.getByTestId("restart-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toContain("1");
    expect(btn.textContent).toContain("重启服务");
  });

  it("restartStatus=polling_health：重启按钮 disabled，文案含 '等待服务恢复'", () => {
    useConfigStore.setState({
      pendingRestartFields: ["model.temperature"],
      restartStatus: "polling_health",
    });
    render(<SaveRestartBar />);
    const btn = screen.getByTestId("restart-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toContain("等待服务恢复");
  });

  it("restartStatus=timeout：显示 '手动刷新' 按钮，点击触发 reload", () => {
    useConfigStore.setState({
      pendingRestartFields: ["model.temperature"],
      restartStatus: "timeout",
      restartError: "健康检查 30s 超时未恢复",
    });
    render(<SaveRestartBar />);
    const btn = screen.getByTestId("manual-reload-button");
    expect(btn).toBeInTheDocument();
    expect(btn.textContent).toContain("手动刷新");
    fireEvent.click(btn);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("restartStatus=ready：500ms 后自动调 reload", () => {
    vi.useFakeTimers();
    render(<SaveRestartBar />);
    // mount 后切到 ready，用 act 包住让 React commit + effect 调度 timer
    act(() => {
      useConfigStore.setState({ restartStatus: "ready" });
    });

    // 还没到 500ms 不 reload
    act(() => {
      vi.advanceTimersByTime(499);
    });
    expect(reloadSpy).toHaveBeenCalledTimes(0);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });
});
