import { describe, expect, it } from "vitest";
import type { ConversationReferenceDTO } from "@/protocol";
import { ConversationReferenceManager } from "../ConversationReferenceManager";

function ref(
  partial: Partial<ConversationReferenceDTO>,
): ConversationReferenceDTO {
  return {
    id: "ref-1",
    kind: "workflow_strategy",
    ref: "workflow_strategy:map_reduce",
    label: "Map Reduce",
    activation: "start_workflow",
    args: {},
    metadata: {},
    ...partial,
  } as ConversationReferenceDTO;
}

describe("ConversationReferenceManager", () => {
  it("injects workflow instruction before the user message", () => {
    const workflow = ref({
      metadata: { mode: "map_reduce" },
    });

    const text = ConversationReferenceManager.prependPromptInjectedReferences(
      "请审查这次改动",
      [workflow],
    );

    expect(text).toBe(
      "必须使用 map_reduce workflow 完成用户需求或任务\n\n请审查这次改动",
    );
  });

  it("uses workflow mode from ref when metadata mode is absent", () => {
    const workflow = ref({
      ref: "workflow_strategy:deep_research",
      label: "Deep Research",
      activation: "guide_payload",
      metadata: {},
    });

    const text = ConversationReferenceManager.prependPromptInjectedReferences(
      "研究上下文压缩方案",
      [workflow],
    );

    expect(text).toBe(
      "必须使用 deep_research workflow 完成用户需求或任务\n\n研究上下文压缩方案",
    );
  });

  it("keeps non-injected references as passthrough", () => {
    const workflow = ref({});
    const skill = ref({
      id: "skill-1",
      kind: "skill",
      ref: "skill:review",
      label: "review",
      activation: "inject_context",
    });
    const command = ref({
      id: "command-1",
      kind: "command",
      ref: "command:builtin.hello",
      label: "Hello",
      activation: "execute_command",
    });

    expect(
      ConversationReferenceManager.passthroughReferences([
        workflow,
        skill,
        command,
      ]),
    ).toEqual([command]);
  });

  it("keeps the legacy skill helper compatible", () => {
    const workflow = ref({
      metadata: { mode: "roundtable_review" },
    });

    expect(
      ConversationReferenceManager.prependPromptInjectedSkills("评审代码", [
        workflow,
      ]),
    ).toBe(
      "必须使用 roundtable_review workflow 完成用户需求或任务\n\n评审代码",
    );
  });
});
