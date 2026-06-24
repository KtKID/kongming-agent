/**
 * ConfigPage 顶级容器最小用例。
 *
 * 覆盖：
 * 1. 无 :section → Navigate 到 /manage/config/model
 * 2. :section=invalid_id → 同上重定向
 * 3. loadStatus=loading → 渲染 "加载配置中..."
 * 4. loadStatus=error → 渲染 "加载失败：xxx"
 * 5. loadStatus=loaded + section=model → 渲染 ModelSection stub
 * 6. 顶部 NavLink 跟随后端 schema.groups 渲染（含正确 href + label）
 * 7. SaveRestartBar 出现在 DOM
 *
 * ## stub 策略
 *
 * 把 section 和 SaveRestartBar 全部 vi.mock 成轻量 stub —— 我们只测装配
 * （是否按 section id 选对了组件，是否传了 fields，是否挂了 bar），section 内部
 * 行为另有专用测试覆盖。
 *
 * ## load 副作用
 *
 * `load()` 是真实 store.load() 内部调 fetch；用例里把 store 状态直接 setState
 * 注入到非 idle，effect 内部 `loadStatus === "idle"` 短路就不会真的 fetch。
 * 唯一例外是用例 1/2（重定向测试），那里 store 默认是 idle，但 Navigate 直接
 * 返回，effect 还是会跑——所以专门 stub load 成 vi.fn() 不让它真的发请求。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { useConfigStore } from "../store";
import type {
  EffectiveResponse,
  FieldMeta,
  RawResponse,
  SchemaResponse,
} from "../types";

// ─── stub sections + SaveRestartBar ───────────────────────────────────────

vi.mock("../sections/ModelSection", () => ({
  ModelSection: ({ fields }: { fields: FieldMeta[] }) => (
    <div data-testid="model-section" data-field-count={fields.length}>
      ModelSection({fields.length})
    </div>
  ),
}));

vi.mock("../sections/RuntimeSection", () => ({
  RuntimeSection: ({ fields }: { fields: FieldMeta[] }) => (
    <div data-testid="runtime-section" data-field-count={fields.length}>
      RuntimeSection({fields.length})
    </div>
  ),
}));

vi.mock("../sections/ToolApprovalSection", () => ({
  ToolApprovalSection: ({ fields }: { fields: FieldMeta[] }) => (
    <div data-testid="tool-approval-section" data-field-count={fields.length}>
      ToolApprovalSection({fields.length})
    </div>
  ),
}));

vi.mock("../sections/SafetySection", () => ({
  SafetySection: () => (
    <div data-testid="safety-section">SafetySection(no-fields)</div>
  ),
}));

vi.mock("../sections/HostObservSection", () => ({
  HostObservSection: ({ fields }: { fields: FieldMeta[] }) => (
    <div data-testid="host-observ-section" data-field-count={fields.length}>
      HostObservSection({fields.length})
    </div>
  ),
}));

vi.mock("../sections/GenericConfigSection", () => ({
  GenericConfigSection: ({
    groupId,
    label,
    fields,
  }: {
    groupId: string;
    label: string;
    fields: FieldMeta[];
  }) => (
    <div
      data-testid="generic-config-section"
      data-group-id={groupId}
      data-label={label}
      data-field-count={fields.length}
    >
      GenericConfigSection({groupId}:{fields.length})
    </div>
  ),
}));

vi.mock("../components/SaveRestartBar", () => ({
  SaveRestartBar: () => <div data-testid="save-restart-bar" />,
}));

// 必须在 vi.mock 之后 import
import { ConfigPage } from "../ConfigPage";

// ─── helpers ──────────────────────────────────────────────────────────────

function makeField(path: string, group: string): FieldMeta {
  return {
    path,
    type: "string",
    editable: true,
    desc: "",
    restart_required: false,
    group,
  };
}

function makeSchema(): SchemaResponse {
  return {
    fields: [
      makeField("model.name", "model"),
      makeField("model.temperature", "model"),
      makeField("runner.max_turns", "runtime"),
      makeField("tool.allow", "tool_approval"),
      makeField("safety.rules", "safety"),
      makeField("host.kind", "host_observ"),
      makeField("workflow.enabled", "workflow"),
      makeField("workflow.scan_interval", "workflow"),
      makeField("sitian.version", "sitian"),
    ],
    groups: [
      { id: "model", label: "模型" },
      { id: "runtime", label: "运行时" },
      { id: "tool_approval", label: "工具与审批" },
      { id: "safety", label: "安全" },
      { id: "host_observ", label: "宿主与可观测" },
      { id: "workflow", label: "工作流" },
      { id: "sitian", label: "司天" },
    ],
  };
}

function makeEffective(): EffectiveResponse {
  return { values: {}, sources: {}, env_overrides: [] };
}

function makeRaw(): RawResponse {
  return { path: "/tmp/setting.yaml", mtime: 0, content: "" };
}

/** 用 MemoryRouter 模拟挂载 `/manage/config[/:section]` 路由。 */
function renderAt(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/manage/config" element={<ConfigPage />} />
        <Route path="/manage/config/:section" element={<ConfigPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useConfigStore.getState().reset();
  // 防止真实 fetch：用 spy 替换 load
  useConfigStore.setState({ load: vi.fn().mockResolvedValue(undefined) });
});

afterEach(() => {
  useConfigStore.getState().reset();
  vi.restoreAllMocks();
});

// ─── 用例 ──────────────────────────────────────────────────────────────

describe("ConfigPage", () => {
  it("无 :section 时重定向到 /manage/config/model", () => {
    // 让 loaded 状态以便 Navigate 目标也能正常渲染（不卡在 loading）
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: makeSchema(),
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config");
    // 重定向落到 model：ModelSection stub 应出现
    expect(screen.getByTestId("model-section")).toBeInTheDocument();
  });

  it("非法 :section 时也重定向到 /manage/config/model", () => {
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: makeSchema(),
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config/invalid_id");
    expect(screen.getByTestId("model-section")).toBeInTheDocument();
  });

  it("loadStatus=loading 时显示 '加载配置中...'", () => {
    useConfigStore.setState({ loadStatus: "loading" });
    renderAt("/manage/config/model");
    expect(screen.getByTestId("config-loading")).toBeInTheDocument();
    expect(screen.getByText("加载配置中...")).toBeInTheDocument();
  });

  it("loadStatus=error 时显示错误信息", () => {
    useConfigStore.setState({
      loadStatus: "error",
      loadError: "fetch failed: 502",
    });
    renderAt("/manage/config/model");
    expect(screen.getByTestId("config-error")).toBeInTheDocument();
    expect(screen.getByText(/fetch failed: 502/)).toBeInTheDocument();
  });

  it("loadStatus=error + loadError=null 时回退到 '未知错误'", () => {
    useConfigStore.setState({ loadStatus: "error", loadError: null });
    renderAt("/manage/config/model");
    expect(screen.getByText(/未知错误/)).toBeInTheDocument();
  });

  it("loaded + section=model 时渲染 ModelSection 且传入 group=model 的 fields", () => {
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: makeSchema(),
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config/model");
    const stub = screen.getByTestId("model-section");
    expect(stub).toBeInTheDocument();
    // schema 中 group=model 的字段有 2 条
    expect(stub.getAttribute("data-field-count")).toBe("2");
  });

  it("loaded + section=safety 时渲染 SafetySection（不传 fields）", () => {
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: makeSchema(),
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config/safety");
    expect(screen.getByTestId("safety-section")).toBeInTheDocument();
    // 其他 section 不应渲染
    expect(screen.queryByTestId("model-section")).toBeNull();
  });

  it("loaded + 未专用实现的 section 时渲染 GenericConfigSection", () => {
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: makeSchema(),
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config/workflow");
    const stub = screen.getByTestId("generic-config-section");
    expect(stub).toBeInTheDocument();
    expect(stub.getAttribute("data-group-id")).toBe("workflow");
    expect(stub.getAttribute("data-label")).toBe("工作流");
    expect(stub.getAttribute("data-field-count")).toBe("2");
  });

  it("顶部 NavLink 跟随后端 schema.groups 渲染，含正确 href + label", () => {
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: makeSchema(),
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config/model");
    const tabs = screen.getByTestId("config-section-tabs");
    const links = tabs.querySelectorAll("a");
    expect(links).toHaveLength(7);
    const expected: Array<[string, string]> = [
      ["/manage/config/model", "模型"],
      ["/manage/config/runtime", "运行时"],
      ["/manage/config/tool_approval", "工具与审批"],
      ["/manage/config/safety", "安全"],
      ["/manage/config/host_observ", "宿主与可观测"],
      ["/manage/config/workflow", "工作流"],
      ["/manage/config/sitian", "司天"],
    ];
    expected.forEach(([href, label], idx) => {
      const a = links[idx]!;
      expect(a.getAttribute("href")).toBe(href);
      expect(a.textContent).toBe(label);
    });
  });

  it("SaveRestartBar 出现在 DOM", () => {
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: makeSchema(),
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config/model");
    expect(screen.getByTestId("save-restart-bar")).toBeInTheDocument();
  });

  it("schema.groups 为空时不渲染 tab 行", () => {
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: { fields: [], groups: [] },
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config/model");
    expect(screen.queryByTestId("config-section-tabs")).toBeNull();
    // SaveRestartBar 仍渲染
    expect(screen.getByTestId("save-restart-bar")).toBeInTheDocument();
  });

  it("schema.groups 为空且 section 非法时重定向到 /manage/config/model", () => {
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: { fields: [makeField("model.name", "model")], groups: [] },
      effective: makeEffective(),
      raw: makeRaw(),
    });
    renderAt("/manage/config/invalid_id");
    expect(screen.getByTestId("model-section")).toBeInTheDocument();
  });

  it("idle 状态时触发 store.load() 一次", () => {
    const loadSpy = vi.fn().mockResolvedValue(undefined);
    useConfigStore.setState({ loadStatus: "idle", load: loadSpy });
    renderAt("/manage/config/model");
    expect(loadSpy).toHaveBeenCalledTimes(1);
  });

  it("非 idle 状态时不触发 store.load()", () => {
    const loadSpy = vi.fn().mockResolvedValue(undefined);
    useConfigStore.setState({
      loadStatus: "loaded",
      schema: makeSchema(),
      effective: makeEffective(),
      raw: makeRaw(),
      load: loadSpy,
    });
    renderAt("/manage/config/model");
    expect(loadSpy).not.toHaveBeenCalled();
  });
});
