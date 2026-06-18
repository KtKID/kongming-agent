import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ChoiceRequestFrame, ChoiceSubmitFrame } from "@/protocol";
import { ChoiceManager, type ChoiceState } from "@/modules/choice/ChoiceManager";
import { ChoicePanel } from "@/modules/choice/ChoicePanel";

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
        options: [
          {
            id: "minimal",
            label: "最小实现",
            description: "先打通主链路。",
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

function Harness({
  onSubmit,
}: {
  onSubmit: (frame: ChoiceSubmitFrame) => void;
}) {
  const [state, setState] = useState<ChoiceState>(() => ChoiceManager.receive(frame()));
  return <ChoicePanel state={state} onChange={setState} onSubmit={onSubmit} />;
}

describe("ChoicePanel", () => {
  it("逐题选择并确认提交", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    await user.click(screen.getByTestId("choice-option-minimal"));
    await user.click(screen.getByTestId("choice-next"));
    await user.click(screen.getByTestId("choice-option-__custom__"));
    await user.type(screen.getByTestId("choice-custom-text"), "只做 Web。");
    await user.click(screen.getByTestId("choice-confirm"));

    expect(onSubmit).toHaveBeenCalledWith({
      frame_type: "choice.submit",
      request_id: "call-1",
      answers: [
        {
          question_id: "scope",
          option_id: "minimal",
          option_label: "最小实现",
          custom_text: null,
          value: null,
        },
        {
          question_id: "note",
          option_id: "__custom__",
          option_label: "自己输入",
          custom_text: "只做 Web。",
        },
      ],
    });
  });

  it("支持 step 回切修改答案", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    await user.click(screen.getByTestId("choice-option-minimal"));
    await user.click(screen.getByTestId("choice-next"));
    await user.click(screen.getByTestId("choice-step-0"));

    expect(screen.getByText("范围")).toBeInTheDocument();
    expect(screen.getByTestId("choice-option-minimal")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
