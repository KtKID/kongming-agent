/**
 * ModelSection 最小用例：
 *
 * 1. effective=null 时整体返回 null（不渲染 section header）
 * 2. 传入 fields 时按数量渲染 FieldRenderer stub
 * 3. raw 存在 → 渲染 YamlPreview（折叠 details）
 *
 * 用 vi.mock 把 FieldRenderer / YamlPreview 替换成 stub，避免被深层组件实现细节
 * 干扰断言（store / value / source 取值路径已经在组件代码里 trivial，本测试只断装配）。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { useConfigStore } from "../../store";
import type { EffectiveResponse, FieldMeta, RawResponse } from "../../types";

vi.mock("../../components/FieldRenderer", () => ({
  FieldRenderer: ({ meta }: { meta: FieldMeta }) => (
    <div data-testid="field-stub" data-path={meta.path}>
      {meta.path}
    </div>
  ),
}));

vi.mock("../../components/YamlPreview", () => ({
  YamlPreview: ({ title }: { title: string }) => (
    <div data-testid="yaml-preview-stub">{title}</div>
  ),
}));

// 必须 import 在 mock 之后
import { ModelSection } from "../ModelSection";

function makeField(path: string): FieldMeta {
  return {
    path,
    type: "string",
    editable: true,
    desc: "",
    restart_required: false,
    group: "model",
  };
}

function makeEffective(paths: string[]): EffectiveResponse {
  const values: Record<string, unknown> = {};
  for (const p of paths) values[p] = "stub";
  return { values, sources: {}, env_overrides: [] };
}

function makeRaw(content: string): RawResponse {
  return { path: "/tmp/setting.yaml", mtime: 0, content };
}

afterEach(() => {
  useConfigStore.getState().reset();
});

describe("ModelSection", () => {
  it("effective=null 时返回 null（不渲染标题）", () => {
    // store 默认 effective=null
    const { container } = render(<ModelSection fields={[]} />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("模型")).toBeNull();
  });

  it("传入 N 个 fields 时渲染对应数量的 FieldRenderer", () => {
    useConfigStore.setState({
      effective: makeEffective([
        "model.name",
        "model.temperature",
        "model.top_p",
      ]),
      raw: null,
    });
    const fields: FieldMeta[] = [
      makeField("model.name"),
      makeField("model.temperature"),
      makeField("model.top_p"),
    ];
    render(<ModelSection fields={fields} />);
    expect(screen.getByText("模型")).toBeInTheDocument();
    expect(screen.getAllByTestId("field-stub")).toHaveLength(3);
    expect(screen.getByText("model.name")).toBeInTheDocument();
    expect(screen.getByText("model.temperature")).toBeInTheDocument();
    expect(screen.getByText("model.top_p")).toBeInTheDocument();
  });

  it("raw 存在时渲染 YamlPreview，raw=null 时不渲染", () => {
    useConfigStore.setState({
      effective: makeEffective(["model.name"]),
      raw: makeRaw("model:\n  name: gpt-4\n"),
    });
    const { rerender } = render(
      <ModelSection fields={[makeField("model.name")]} />,
    );
    expect(screen.getByTestId("yaml-preview-stub")).toBeInTheDocument();
    expect(screen.getByText("model 段 yaml 原文")).toBeInTheDocument();

    // 切到 raw=null
    useConfigStore.setState({
      effective: makeEffective(["model.name"]),
      raw: null,
    });
    rerender(<ModelSection fields={[makeField("model.name")]} />);
    expect(screen.queryByTestId("yaml-preview-stub")).toBeNull();
  });
});
