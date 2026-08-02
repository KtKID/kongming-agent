/** SafetySection 装配测试：字段透传给全局安全配置视图。 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FieldMeta } from "../../types";

vi.mock("../../components/SafetyRulesView", () => ({
  SafetyRulesView: ({ fields }: { fields: FieldMeta[] }) => (
    <div data-testid="safety-rules-view-stub" data-field-count={fields.length} />
  ),
}));

import { SafetySection } from "../SafetySection";

const fields: FieldMeta[] = [
  {
    path: "safety.approval.llm",
    type: "dict",
    editable: false,
    desc: "LLM 复核器",
    restart_required: true,
    group: "safety",
  },
];

describe("SafetySection", () => {
  it("说明模式按项目目录保存，并把 schema fields 交给视图", () => {
    render(<SafetySection fields={fields} />);
    expect(screen.getByText("处置模式按项目目录选择", { exact: false })).toBeInTheDocument();
    expect(screen.getByTestId("safety-rules-view-stub")).toHaveAttribute(
      "data-field-count",
      "1",
    );
  });
});
