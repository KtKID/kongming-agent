/**
 * FieldRenderer 最小用例：
 *
 * 1. type=string + editable=true：渲染 `<input>`，输入触发 onChange
 * 2. type=bool   + editable=true：渲染 Switch（role=switch），点击触发 onChange(true/false)
 * 3. type=enum   + editable=true：渲染 `<select>`，选项数 = meta.enum.length
 * 4. editable=false：不渲染可编辑控件（无 input / 无 switch），点击不会触发 onChange
 * 5. isDirty=true：显示"撤销"按钮，点击触发 onClear
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { FieldRenderer } from "../FieldRenderer";
import type { FieldMeta, FieldSource } from "../../types";

function makeMeta(overrides: Partial<FieldMeta> = {}): FieldMeta {
  return {
    path: "model.temperature",
    type: "string",
    editable: true,
    desc: "采样温度",
    restart_required: false,
    group: "model",
    ...overrides,
  };
}

const NOOP = () => {};

interface RenderArgs {
  meta: FieldMeta;
  value: unknown;
  source?: FieldSource;
  isDirty?: boolean;
  onChange?: (v: unknown) => void;
  onClear?: () => void;
}

function renderField(args: RenderArgs) {
  return render(
    <FieldRenderer
      meta={args.meta}
      value={args.value}
      source={args.source ?? "default"}
      isDirty={args.isDirty ?? false}
      onChange={args.onChange ?? NOOP}
      onClear={args.onClear ?? NOOP}
    />,
  );
}

describe("FieldRenderer", () => {
  it("type=string + editable=true：渲染 input，输入触发 onChange", () => {
    const onChange = vi.fn();
    renderField({
      meta: makeMeta({ type: "string", path: "model.name" }),
      value: "hello",
      onChange,
    });

    const input = screen.getByLabelText("model.name") as HTMLInputElement;
    expect(input.tagName).toBe("INPUT");
    expect(input.type).toBe("text");
    expect(input.value).toBe("hello");

    fireEvent.change(input, { target: { value: "world" } });
    expect(onChange).toHaveBeenCalledWith("world");
  });

  it("type=bool + editable=true：渲染 Switch，点击触发 onChange(true/false)", () => {
    const onChange = vi.fn();
    const { rerender } = renderField({
      meta: makeMeta({ type: "bool", path: "feature.enabled" }),
      value: false,
      onChange,
    });

    const sw = screen.getByRole("switch", { name: "feature.enabled" });
    expect(sw.getAttribute("aria-checked")).toBe("false");

    fireEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);

    // 切回 checked=true 再点一次，验证 onChange(false)
    rerender(
      <FieldRenderer
        meta={makeMeta({ type: "bool", path: "feature.enabled" })}
        value={true}
        source="default"
        isDirty={false}
        onChange={onChange}
        onClear={NOOP}
      />,
    );
    const sw2 = screen.getByRole("switch", { name: "feature.enabled" });
    expect(sw2.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(sw2);
    expect(onChange).toHaveBeenLastCalledWith(false);
  });

  it("type=enum + editable=true：渲染 select，选项数 = meta.enum.length", () => {
    const onChange = vi.fn();
    const enumValues = ["low", "medium", "high"];
    renderField({
      meta: makeMeta({
        type: "enum",
        path: "reasoning.effort",
        enum: enumValues,
      }),
      value: "low",
      onChange,
    });

    const select = screen.getByLabelText(
      "reasoning.effort",
    ) as HTMLSelectElement;
    expect(select.tagName).toBe("SELECT");
    expect(select.options.length).toBe(enumValues.length);
    expect(Array.from(select.options).map((o) => o.value)).toEqual(enumValues);

    fireEvent.change(select, { target: { value: "high" } });
    expect(onChange).toHaveBeenCalledWith("high");
  });

  it("editable=false：不渲染可编辑控件，且提示文案出现", () => {
    const onChange = vi.fn();
    renderField({
      meta: makeMeta({
        type: "string",
        path: "host.name",
        editable: false,
      }),
      value: "fixed",
      onChange,
    });

    // 既无 input 也无 switch 控件
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    // 提示文案
    expect(screen.getByText("本字段不可在 UI 修改")).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("isDirty=true：显示撤销按钮，点击触发 onClear", () => {
    const onClear = vi.fn();
    renderField({
      meta: makeMeta({ type: "string", path: "model.temperature" }),
      value: "0.7",
      isDirty: true,
      onClear,
    });

    const btn = screen.getByRole("button", { name: "撤销 model.temperature" });
    expect(btn).toBeInTheDocument();
    // 脏标记文案
    expect(screen.getByText("已改")).toBeInTheDocument();

    fireEvent.click(btn);
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});
