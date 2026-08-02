/**
 * SafetyRulesView 全局安全配置测试。
 *
 * 验证只渲染 LLM 复核器配置，并把每 cwd 的模式职责留给聊天页选择器。
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useConfigStore } from "../../store";
import type { FieldMeta } from "../../types";
import { SafetyRulesView } from "../SafetyRulesView";

const fields: FieldMeta[] = [
  {
    path: "safety.approval.llm",
    type: "dict",
    editable: false,
    desc: "LLM 复核器",
    restart_required: true,
    group: "safety",
  },
  {
    path: "safety.permissions.allow",
    type: "list",
    editable: true,
    desc: "旧全局允许规则",
    restart_required: false,
    group: "safety",
  },
];

afterEach(() => {
  cleanup();
  useConfigStore.getState().reset();
});

describe("SafetyRulesView", () => {
  it("只渲染 LLM 复核器，旧全局 permissions 字段退出视图", () => {
    useConfigStore.setState({
      effective: {
        values: {
          "safety.approval.llm": { model: "review-model" },
          "safety.permissions.allow": ["read_file"],
        },
        sources: {
          "safety.approval.llm": "yaml",
          "safety.permissions.allow": "yaml",
        },
        env_overrides: [],
      },
    });

    render(<SafetyRulesView fields={fields} />);

    expect(screen.getByTestId("global-safety-settings")).toHaveTextContent(
      '"model": "review-model"',
    );
    expect(document.querySelector('[data-field-path="safety.permissions.allow"]')).toBeNull();
    expect(screen.queryByText("read_file")).toBeNull();
  });

  it("LLM 复核器配置保持只读，避免局部字段写回破坏嵌套结构", () => {
    useConfigStore.setState({
      effective: {
        values: {
          "safety.approval.llm": { model: "review-model" },
        },
        sources: {
          "safety.approval.llm": "yaml",
        },
        env_overrides: [],
      },
    });

    render(<SafetyRulesView fields={fields} />);
    expect(useConfigStore.getState().dirty).toEqual({});
  });
});
