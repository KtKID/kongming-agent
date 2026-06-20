import type {
  ChoiceAnswerDTO,
  ChoiceOptionDTO,
  ChoiceQuestionDTO,
  ChoiceRequestFrame,
  ChoiceSubmitFrame,
} from "@/protocol";

export const CUSTOM_CHOICE_OPTION_ID = "__custom__";

export interface ChoiceDraftAnswer {
  optionId: string | null;
  customText: string;
}

export interface ChoiceState {
  request: ChoiceRequestFrame;
  activeIndex: number;
  answers: Record<string, ChoiceDraftAnswer>;
}

const CUSTOM_OPTION: ChoiceOptionDTO = {
  id: CUSTOM_CHOICE_OPTION_ID,
  label: "自己输入",
  description: "填写一个更贴合当前情况的答案。",
};

export class ChoiceManager {
  static receive(frame: ChoiceRequestFrame): ChoiceState {
    return {
      request: frame,
      activeIndex: 0,
      answers: Object.fromEntries(
        frame.questions.map((question) => [
          question.id,
          { optionId: null, customText: "" },
        ]),
      ),
    };
  }

  static optionsForQuestion(question: ChoiceQuestionDTO): ChoiceOptionDTO[] {
    return [...question.options, CUSTOM_OPTION];
  }

  static select(
    state: ChoiceState,
    questionId: string,
    optionId: string,
  ): ChoiceState {
    const existing = state.answers[questionId] ?? { optionId: null, customText: "" };
    return {
      ...state,
      answers: {
        ...state.answers,
        [questionId]: {
          ...existing,
          optionId,
        },
      },
    };
  }

  static setCustomText(
    state: ChoiceState,
    questionId: string,
    customText: string,
  ): ChoiceState {
    const existing = state.answers[questionId] ?? { optionId: null, customText: "" };
    return {
      ...state,
      answers: {
        ...state.answers,
        [questionId]: {
          ...existing,
          optionId: CUSTOM_CHOICE_OPTION_ID,
          customText,
        },
      },
    };
  }

  static goTo(state: ChoiceState, index: number): ChoiceState {
    const max = Math.max(0, state.request.questions.length - 1);
    return {
      ...state,
      activeIndex: Math.min(Math.max(index, 0), max),
    };
  }

  static next(state: ChoiceState): ChoiceState {
    return ChoiceManager.goTo(state, state.activeIndex + 1);
  }

  static previous(state: ChoiceState): ChoiceState {
    return ChoiceManager.goTo(state, state.activeIndex - 1);
  }

  static isQuestionValid(state: ChoiceState, questionId: string): boolean {
    const answer = state.answers[questionId];
    if (!answer?.optionId) return false;
    if (answer.optionId === CUSTOM_CHOICE_OPTION_ID) {
      return answer.customText.trim().length > 0;
    }
    return true;
  }

  static canSubmit(state: ChoiceState): boolean {
    return state.request.questions.every((question) =>
      ChoiceManager.isQuestionValid(state, question.id),
    );
  }

  static buildSubmitFrame(state: ChoiceState): ChoiceSubmitFrame {
    if (!ChoiceManager.canSubmit(state)) {
      throw new Error("choice answers are incomplete");
    }
    const answers: ChoiceAnswerDTO[] = state.request.questions.map((question) => {
      const draft = state.answers[question.id];
      const optionId = draft.optionId ?? "";
      if (optionId === CUSTOM_CHOICE_OPTION_ID) {
        return {
          question_id: question.id,
          option_id: CUSTOM_CHOICE_OPTION_ID,
          option_label: CUSTOM_OPTION.label,
          custom_text: draft.customText.trim(),
        };
      }
      const option = question.options.find((item) => item.id === optionId);
      if (!option) {
        throw new Error(`unknown option id: ${optionId}`);
      }
      return {
        question_id: question.id,
        option_id: option.id,
        option_label: option.label,
        custom_text: null,
        value: option.value ?? null,
      };
    });
    return {
      frame_type: "choice.submit",
      request_id: state.request.request_id,
      answers,
    };
  }
}
