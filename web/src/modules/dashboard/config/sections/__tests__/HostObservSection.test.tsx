/**
 * HostObservSection 最小用例：
 *
 * 1. effective=null 返回 null
 * 2. 字段按 host / web / trace / logging / evolution 分桶
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

import { HostObservSection } from "../HostObservSection";

function makeField(path: string, type: FieldMeta["type"] = "string"): FieldMeta {
  return {
    path,
    type,
    editable: true,
    desc: "",
    restart_required: false,
    group: "host_observ",
  };
}

function makeEffective(paths: string[]): EffectiveResponse {
  const values: Record<string, unknown> = {};
  for (const p of paths) values[p] = null;
  return { values, sources: {}, env_overrides: [] };
}

afterEach(() => {
  useConfigStore.getState().reset();
});

describe("HostObservSection", () => {
  it("effective=null 时返回 null", () => {
    const { container } = render(<HostObservSection fields={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("按 host / web / trace / logging / evolution 分桶", () => {
    const fields: FieldMeta[] = [
      makeField("host.kind"),
      makeField("web.port", "int"),
      makeField("web.llm_presets", "list"),
      makeField("trace.output_path"),
      makeField("logging.level"),
      makeField("evolution.root"),
    ];
    useConfigStore.setState({
      effective: makeEffective(fields.map((f) => f.path)),
    });
    render(<HostObservSection fields={fields} />);
    expect(screen.getByText("宿主与可观测")).toBeInTheDocument();
    expect(screen.getAllByTestId("field-stub")).toHaveLength(fields.length);

    const buckets = document.querySelectorAll("[data-hostobserv-bucket]");
    const presentPrefixes = Array.from(buckets).map((el) =>
      el.getAttribute("data-hostobserv-bucket"),
    );
    expect(presentPrefixes).toEqual([
      "host",
      "web",
      "trace",
      "logging",
      "evolution",
    ]);
  });

  it("未知前缀字段进入 __other__ 桶", () => {
    const fields: FieldMeta[] = [makeField("unknown.x")];
    useConfigStore.setState({
      effective: makeEffective(fields.map((f) => f.path)),
    });
    render(<HostObservSection fields={fields} />);
    const other = document.querySelector(
      '[data-hostobserv-bucket="__other__"]',
    );
    expect(other).not.toBeNull();
    expect(screen.getByText("unknown.x")).toBeInTheDocument();
  });
});
