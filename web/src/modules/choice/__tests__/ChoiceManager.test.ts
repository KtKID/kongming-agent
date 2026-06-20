import { describe, expect, it } from "vitest";
import type { ChoiceRequestFrame } from "@/protocol";
import {
  CUSTOM_CHOICE_OPTION_ID,
  ChoiceManager,
} from "@/modules/choice/ChoiceManager";

function frame(): ChoiceRequestFrame {
  return {
    frame_type: "choice.request",
    timestamp_ms: 1,
    request_id: "call-1",
    title: "选择方案",
    description: "请选择下一步。",
    turn: 2,
    run_id: "run-1",
    questions: [
      {
        id: "scope",
        title: "范围",
        description: "控制范围。",
        options: [
          {
            id: "minimal",
            label: "最小实现",
            description: "先打通主链路。",
            value: { scope: "minimal" },
          },
        ],
      },
      {
        id: "note",
        title: "补充",
        options: [
          {
            id: "skip",
            label: "无补充",
            description: "直接继续。",
          },
        ],
      },
    ],
  };
}

describe("ChoiceManager", () => {
  it("receive 初始化逐题答案草稿", () => {
    const state = ChoiceManager.receive(frame());

    expect(state.activeIndex).toBe(0);
    expect(state.answers.scope.optionId).toBeNull();
    expect(state.answers.note.customText).toBe("");
  });

  it("optionsForQuestion 固定追加自定义输入选项", () => {
    const state = ChoiceManager.receive(frame());
    const options = ChoiceManager.optionsForQuestion(state.request.questions[0]);

    expect(options.map((item) => item.id)).toEqual([
      "minimal",
      CUSTOM_CHOICE_OPTION_ID,
    ]);
  });

  it("select + custom text 后生成 choice.submit", () => {
    let state = ChoiceManager.receive(frame());
    state = ChoiceManager.select(state, "scope", "minimal");
    state = ChoiceManager.setCustomText(state, "note", "只做 Web。");

    expect(ChoiceManager.canSubmit(state)).toBe(true);
    expect(ChoiceManager.buildSubmitFrame(state)).toEqual({
      frame_type: "choice.submit",
      request_id: "call-1",
      answers: [
        {
          question_id: "scope",
          option_id: "minimal",
          option_label: "最小实现",
          custom_text: null,
          value: { scope: "minimal" },
        },
        {
          question_id: "note",
          option_id: CUSTOM_CHOICE_OPTION_ID,
          option_label: "自己输入",
          custom_text: "只做 Web。",
        },
      ],
    });
  });

  it("自定义输入为空时不能提交", () => {
    let state = ChoiceManager.receive(frame());
    state = ChoiceManager.select(state, "scope", "minimal");
    state = ChoiceManager.select(state, "note", CUSTOM_CHOICE_OPTION_ID);

    expect(ChoiceManager.canSubmit(state)).toBe(false);
  });
});
