/**
 * ToolApprovalSection 最小用例：
 *
 * 1. effective=null 返回 null
 * 2. 字段按 tool / approval / scheduler 分桶，空桶不显示
 * 3. 未知前缀走 __other__ 兜底
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { useConfigStore } from "../../store";
import type { EffectiveResponse, FieldMeta } from "../../types";

vi.mock("../../components/FieldRenderer", () => ({
  FieldRenderer: ({ meta }: { meta: FieldMeta }) => (
    <div data-testid="field-stub" data-path={meta.path}>
      {meta.path}
    </div>
  ),
}));

import { ToolApprovalSection } from "../ToolApprovalSection";

function makeField(path: string): FieldMeta {
  return {
    path,
    type: "bool",
    editable: true,
    desc: "",
    restart_required: false,
    group: "tool_approval",
  };
}

function makeEffective(paths: string[]): EffectiveResponse {
  const values: Record<string, unknown> = {};
  for (const p of paths) values[p] = false;
  return { values, sources: {}, env_overrides: [] };
}

afterEach(() => {
  useConfigStore.getState().reset();
});

describe("ToolApprovalSection", () => {
  it("effective=null 时返回 null", () => {
    const { container } = render(<ToolApprovalSection fields={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("按前缀分桶 tool / approval / scheduler", () => {
    const fields: FieldMeta[] = [
      makeField("tool.read.enabled"),
      makeField("tool.shell.enabled"),
      makeField("approval.mode"),
      makeField("scheduler.tick_interval"),
    ];
    useConfigStore.setState({
      effective: makeEffective(fields.map((f) => f.path)),
    });
    render(<ToolApprovalSection fields={fields} />);
    expect(screen.getByText("工具与审批")).toBeInTheDocument();
    expect(screen.getAllByTestId("field-stub")).toHaveLength(fields.length);

    const buckets = document.querySelectorAll("[data-toolapproval-bucket]");
    const presentPrefixes = Array.from(buckets).map((el) =>
      el.getAttribute("data-toolapproval-bucket"),
    );
    expect(presentPrefixes).toEqual(["tool", "approval", "scheduler"]);
  });

  it("未知前缀字段进入 __other__ 桶", () => {
    const fields: FieldMeta[] = [makeField("misc.x")];
    useConfigStore.setState({
      effective: makeEffective(fields.map((f) => f.path)),
    });
    render(<ToolApprovalSection fields={fields} />);
    const other = document.querySelector(
      '[data-toolapproval-bucket="__other__"]',
    );
    expect(other).not.toBeNull();
    expect(screen.getByText("misc.x")).toBeInTheDocument();
  });
});
