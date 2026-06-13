/**
 * SafetySection 最小用例：
 *
 * 1. effective / raw 任一为 null → 显示加载文案，不渲染 SafetyRulesView
 * 2. 二者就绪 → 渲染 SafetyRulesView stub
 * 3. **绝不渲染任何编辑控件**（无 input / 无 switch / 无撤销按钮）
 *
 * SafetyRulesView 用 vi.mock 替成 stub —— 本 section 自身职责只是装配 store +
 * 加固定说明文案 + 加载态分支，SafetyRulesView 自身行为已经在它的单测里覆盖。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { useConfigStore } from "../../store";
import type { EffectiveResponse, RawResponse } from "../../types";

vi.mock("../../components/SafetyRulesView", () => ({
  SafetyRulesView: () => (
    <div data-testid="safety-rules-view-stub">SafetyRulesView</div>
  ),
}));

import { SafetySection } from "../SafetySection";

function makeEffective(): EffectiveResponse {
  return {
    values: {
      "safety.hard_deny_commands": [],
      "safety.log_silent_reads": false,
    },
    sources: {},
    env_overrides: [],
  };
}

function makeRaw(): RawResponse {
  return {
    path: "/tmp/setting.yaml",
    mtime: 0,
    content: "safety:\n  log_silent_reads: false\n",
  };
}

afterEach(() => {
  useConfigStore.getState().reset();
});

describe("SafetySection", () => {
  it("effective=null / raw=null 时显示加载文案", () => {
    render(<SafetySection />);
    expect(screen.getByText("安全")).toBeInTheDocument();
    expect(
      screen.getByText("安全规则一期全量只读。要修改请直接编辑 setting.yaml 后点重启。"),
    ).toBeInTheDocument();
    expect(screen.getByText("配置加载中…")).toBeInTheDocument();
    expect(screen.queryByTestId("safety-rules-view-stub")).toBeNull();
  });

  it("只 effective 就绪时仍显示加载", () => {
    useConfigStore.setState({ effective: makeEffective(), raw: null });
    render(<SafetySection />);
    expect(screen.getByText("配置加载中…")).toBeInTheDocument();
    expect(screen.queryByTestId("safety-rules-view-stub")).toBeNull();
  });

  it("effective + raw 同时就绪 → 渲染 SafetyRulesView", () => {
    useConfigStore.setState({ effective: makeEffective(), raw: makeRaw() });
    render(<SafetySection />);
    expect(screen.getByTestId("safety-rules-view-stub")).toBeInTheDocument();
    expect(screen.queryByText("配置加载中…")).toBeNull();
  });

  it("不渲染任何编辑控件（无 input / switch / 撤销按钮）", () => {
    useConfigStore.setState({ effective: makeEffective(), raw: makeRaw() });
    render(<SafetySection />);
    // section 自己的 DOM 里不能有这些
    const section = document.querySelector('[data-section="safety"]');
    expect(section).not.toBeNull();
    expect(section!.querySelector("input")).toBeNull();
    expect(section!.querySelector('[role="switch"]')).toBeNull();
    expect(section!.querySelector("button")).toBeNull();
  });
});
