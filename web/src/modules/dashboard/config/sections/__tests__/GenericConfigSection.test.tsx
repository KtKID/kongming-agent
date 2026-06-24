/**
 * GenericConfigSection 最小用例：
 *
 * 1. effective=null 时返回 null
 * 2. 字段按 path 分桶
 * 3. 空 path 进入 __other__ 兜底桶
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

import { useConfigStore } from "../../store";
import type { EffectiveResponse, FieldMeta } from "../../types";

vi.mock("../../components/FieldRenderer", () => ({
  FieldRenderer: ({ meta }: { meta: FieldMeta }) => (
    <div data-testid="field-stub" data-path={meta.path}>
      {meta.path || "<empty>"}
    </div>
  ),
}));

import { GenericConfigSection } from "../GenericConfigSection";

function makeField(path: string): FieldMeta {
  return {
    path,
    type: "string",
    editable: true,
    desc: "",
    restart_required: false,
    group: "workflow",
  };
}

function makeEffective(paths: string[]): EffectiveResponse {
  const values: Record<string, unknown> = {};
  for (const p of paths) values[p] = "stub";
  return { values, sources: {}, env_overrides: [] };
}

afterEach(() => {
  act(() => {
    useConfigStore.getState().reset();
  });
});

describe("GenericConfigSection", () => {
  it("effective=null 时返回 null", () => {
    const { container } = render(
      <GenericConfigSection groupId="workflow" label="工作流" fields={[]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("按 path 分桶", () => {
    const fields = [
      makeField("workflow.enabled"),
      makeField("sitian.scanner.recent_session_window_days"),
    ];
    act(() => {
      useConfigStore.setState({
        effective: makeEffective(fields.map((f) => f.path)),
      });
    });

    render(
      <GenericConfigSection
        groupId="workflow"
        label="工作流"
        fields={fields}
      />,
    );

    expect(
      document.querySelector('[data-generic-config-bucket="workflow"]'),
    ).not.toBeNull();
    expect(
      document.querySelector('[data-generic-config-bucket="sitian.scanner"]'),
    ).not.toBeNull();
    expect(screen.getAllByTestId("field-stub")).toHaveLength(2);
  });

  it("空 path 进入 __other__ 兜底桶", () => {
    const fields = [makeField("")];
    act(() => {
      useConfigStore.setState({
        effective: makeEffective(fields.map((f) => f.path)),
      });
    });

    render(
      <GenericConfigSection
        groupId="workflow"
        label="工作流"
        fields={fields}
      />,
    );

    expect(
      document.querySelector('[data-generic-config-bucket="__other__"]'),
    ).not.toBeNull();
    expect(screen.getByText("<empty>")).toBeInTheDocument();
  });
});
