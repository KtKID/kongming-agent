/**
 * RuntimeSection 最小用例：
 *
 * 1. effective=null 时返回 null
 * 2. 多前缀 fields → 按 runner / session / compactor / retry / stream / cli / 其他
 *    分子桶；空桶不渲染 h4
 * 3. 未知前缀字段进入兜底"其他"桶
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

import { RuntimeSection } from "../RuntimeSection";

function makeField(path: string): FieldMeta {
  return {
    path,
    type: "int",
    editable: true,
    desc: "",
    restart_required: false,
    group: "runtime",
  };
}

function makeEffective(paths: string[]): EffectiveResponse {
  const values: Record<string, unknown> = {};
  for (const p of paths) values[p] = 0;
  return { values, sources: {}, env_overrides: [] };
}

afterEach(() => {
  useConfigStore.getState().reset();
});

describe("RuntimeSection", () => {
  it("effective=null 时返回 null", () => {
    const { container } = render(<RuntimeSection fields={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("按前缀分桶；空桶 h4 不渲染", () => {
    const fields: FieldMeta[] = [
      makeField("runner.max_turns"),
      makeField("runner.timeout_seconds"),
      makeField("session.backend"),
      makeField("compactor.enabled"),
      makeField("cli.show_reasoning"),
    ];
    useConfigStore.setState({
      effective: makeEffective(fields.map((f) => f.path)),
    });
    render(<RuntimeSection fields={fields} />);

    expect(screen.getByText("运行时")).toBeInTheDocument();
    expect(screen.getAllByTestId("field-stub")).toHaveLength(fields.length);

    // 各 bucket DOM 属性挂上了，顺序与 SUB_GROUPS 定义一致
    const buckets = document.querySelectorAll("[data-runtime-bucket]");
    const presentPrefixes = Array.from(buckets).map((el) =>
      el.getAttribute("data-runtime-bucket"),
    );
    expect(presentPrefixes).toEqual([
      "runner",
      "session",
      "compactor",
      "cli",
    ]);
    // h4 label 出现（用 h4 query 避开 field-stub 文本 collision）
    const headings = Array.from(document.querySelectorAll("h4")).map(
      (h) => h.textContent ?? "",
    );
    expect(headings.some((t) => t.startsWith("runner"))).toBe(true);
    expect(headings.some((t) => t.startsWith("session"))).toBe(true);
    expect(headings.some((t) => t.startsWith("compactor"))).toBe(true);
    expect(headings.some((t) => t.startsWith("cli"))).toBe(true);
    expect(headings.some((t) => t.startsWith("retry"))).toBe(false);
    expect(headings.some((t) => t.startsWith("stream"))).toBe(false);
  });

  it("未知前缀字段进入 __other__ 桶", () => {
    const fields: FieldMeta[] = [makeField("foobar.x"), makeField("xxx.y")];
    useConfigStore.setState({
      effective: makeEffective(fields.map((f) => f.path)),
    });
    render(<RuntimeSection fields={fields} />);
    const otherBucket = document.querySelector(
      '[data-runtime-bucket="__other__"]',
    );
    expect(otherBucket).not.toBeNull();
    expect(screen.getByText("foobar.x")).toBeInTheDocument();
    expect(screen.getByText("xxx.y")).toBeInTheDocument();
  });
});
