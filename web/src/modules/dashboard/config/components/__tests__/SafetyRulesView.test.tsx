/**
 * SafetyRulesView 只读视图最小用例。
 *
 * 覆盖：
 * - 空 safety 段：8 个 panel 全部显示空提示
 * - 含一条 hard_deny：表格渲染 name / matcher / reason 三列文本
 * - trusted_workdirs：list[str] 渲染为路径列表
 * - log_silent_reads=true：开关展示已开 + aria-disabled（不可点击语义）
 * - extractSafetySection：能正确切出 safety 段子串，不混入其他顶层 key
 * - YamlPreview 出现在 DOM 里（折叠默认收起，但 <details> 一定存在）
 */

import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import type { EffectiveResponse, RawResponse } from "../../types";

import {
  SafetyRulesView,
  extractSafetySection,
} from "../SafetyRulesView";

function emptyEffective(extra: Record<string, unknown> = {}): EffectiveResponse {
  return {
    values: {
      "safety.hard_deny_commands": [],
      "safety.approval_required_commands": [],
      "safety.sensitive_paths": [],
      "safety.skill_call_rules": [],
      "safety.trusted_workdirs": [],
      "safety.allow_writes": [],
      "safety.allow_tools_silent": [],
      "safety.log_silent_reads": false,
      ...extra,
    },
    sources: {},
    env_overrides: [],
  };
}

function makeRaw(content: string): RawResponse {
  return { path: "/tmp/setting.yaml", mtime: 0, content };
}

const SAMPLE_YAML = `# top header
model:
  name: gpt-4
  temperature: 0.7
safety:
  hard_deny_commands:
    - name: deny-rm-rf
      matcher: '^rm -rf /'
  log_silent_reads: false
runtime:
  max_turns: 20
`;

describe("SafetyRulesView · 空段", () => {
  it("8 个子段全部显示 '(无自定义规则，仅默认基线)'，log_silent_reads 单值除外", () => {
    render(
      <SafetyRulesView
        effective={emptyEffective()}
        raw={makeRaw(SAMPLE_YAML)}
      />,
    );

    // 7 个 list 段都该出现空提示（log_silent_reads 是 bool，不算）
    const empties = screen.getAllByText("（无自定义规则，仅默认基线）");
    expect(empties).toHaveLength(7);

    // 每个 panel 都带"一期只读"hint
    const hints = screen.getAllByText("一期只读，编辑能力在后续版本开放");
    expect(hints).toHaveLength(8);
  });
});

describe("SafetyRulesView · 含规则", () => {
  it("hard_deny_commands 表格渲染 name / matcher / reason", () => {
    const effective = emptyEffective({
      "safety.hard_deny_commands": [
        {
          name: "deny-rm-rf",
          matcher: "^rm -rf /",
          match_mode: "segment_regex",
          boundary_scope: "any",
          reason: "防止根目录删除",
        },
      ],
    });
    render(
      <SafetyRulesView effective={effective} raw={makeRaw(SAMPLE_YAML)} />,
    );
    const panel = screen.getByLabelText("hard_deny_commands");
    expect(within(panel).getByText("deny-rm-rf")).toBeInTheDocument();
    expect(within(panel).getByText("^rm -rf /")).toBeInTheDocument();
    expect(within(panel).getByText("防止根目录删除")).toBeInTheDocument();
    // 表格存在
    expect(panel.querySelector("table")).not.toBeNull();
  });

  it("trusted_workdirs 列表渲染路径条目", () => {
    const effective = emptyEffective({
      "safety.trusted_workdirs": [
        "/Volumes/machub_app/proj/kongming-agent",
        "/Users/kid/sandbox",
      ],
    });
    render(
      <SafetyRulesView effective={effective} raw={makeRaw(SAMPLE_YAML)} />,
    );
    const panel = screen.getByLabelText("trusted_workdirs");
    expect(
      within(panel).getByText("/Volumes/machub_app/proj/kongming-agent"),
    ).toBeInTheDocument();
    expect(within(panel).getByText("/Users/kid/sandbox")).toBeInTheDocument();
  });

  it("log_silent_reads=true：开关 aria-checked=true 且 aria-disabled", () => {
    const effective = emptyEffective({
      "safety.log_silent_reads": true,
    });
    render(
      <SafetyRulesView effective={effective} raw={makeRaw(SAMPLE_YAML)} />,
    );
    const sw = screen.getByRole("switch");
    expect(sw).toHaveAttribute("aria-checked", "true");
    // aria-disabled 表达"不可交互"语义（一期只读）
    expect(sw).toHaveAttribute("aria-disabled");
  });
});

describe("extractSafetySection", () => {
  it("抽出 safety 段，包含子段名，不含 model / runtime 顶层 key", () => {
    const out = extractSafetySection(SAMPLE_YAML);
    expect(out).toContain("safety:");
    expect(out).toContain("hard_deny_commands");
    expect(out).toContain("log_silent_reads");
    // 不能掺到下一段（runtime / model 都是顶层 key）
    expect(out).not.toContain("max_turns");
    expect(out).not.toMatch(/^model:/m);
  });

  it("整篇无 safety 段时返回占位文本", () => {
    const out = extractSafetySection("model:\n  name: gpt-4\n");
    expect(out).toBe("（无 safety 段）");
  });

  it("空字符串返回占位文本", () => {
    expect(extractSafetySection("")).toBe("（无 safety 段）");
  });

  it("safety 是最后一段时切到文件末尾", () => {
    const yaml = "model:\n  name: x\nsafety:\n  log_silent_reads: true\n";
    const out = extractSafetySection(yaml);
    expect(out).toContain("log_silent_reads: true");
    expect(out).not.toContain("model:");
  });
});

describe("SafetyRulesView · YamlPreview 组合", () => {
  it("底部 YamlPreview 出现在 DOM（折叠态，<details> + summary 在）", () => {
    const { container } = render(
      <SafetyRulesView
        effective={emptyEffective()}
        raw={makeRaw(SAMPLE_YAML)}
      />,
    );
    const details = container.querySelector("details");
    expect(details).not.toBeNull();
    expect(screen.getByText("safety 段 yaml 原文")).toBeInTheDocument();
  });
});
