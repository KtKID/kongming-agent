import { Check, ChevronLeft, ChevronRight, PencilLine } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { ChoiceSubmitFrame } from "@/protocol";
import {
  CUSTOM_CHOICE_OPTION_ID,
  ChoiceManager,
  type ChoiceState,
} from "@/modules/choice/ChoiceManager";
import { cn } from "@/lib/utils";

interface ChoicePanelProps {
  state: ChoiceState;
  disabled?: boolean;
  onChange: (state: ChoiceState) => void;
  onSubmit: (frame: ChoiceSubmitFrame) => void | Promise<void>;
}

export function ChoicePanel({
  state,
  disabled = false,
  onChange,
  onSubmit,
}: ChoicePanelProps) {
  const question = state.request.questions[state.activeIndex];
  if (!question) return null;

  const answer = state.answers[question.id] ?? {
    optionId: null,
    customText: "",
  };
  const options = ChoiceManager.optionsForQuestion(question);
  const currentValid = ChoiceManager.isQuestionValid(state, question.id);
  const canSubmit = ChoiceManager.canSubmit(state);
  const isFirst = state.activeIndex === 0;
  const isLast = state.activeIndex === state.request.questions.length - 1;

  const submit = () => {
    if (!canSubmit || disabled) return;
    void onSubmit(ChoiceManager.buildSubmitFrame(state));
  };

  return (
    <section
      className="border-t border-border/60 bg-background/10 px-2 pt-2"
      data-testid="choice-panel"
      aria-label={state.request.title}
    >
      <div className="rounded-lg border border-border/70 bg-card/80 p-3 shadow-sm">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-foreground">
              {state.request.title}
            </h2>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {state.request.description}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1" data-testid="choice-steps">
            {state.request.questions.map((item, index) => {
              const selected = index === state.activeIndex;
              const valid = ChoiceManager.isQuestionValid(state, item.id);
              return (
                <button
                  key={item.id}
                  type="button"
                  className={cn(
                    "flex h-7 min-w-7 items-center justify-center rounded-md border px-2 text-xs font-medium transition-colors",
                    selected
                      ? "border-primary bg-primary text-primary-foreground"
                      : valid
                        ? "border-primary/30 bg-primary/10 text-primary"
                        : "border-border bg-background/60 text-muted-foreground",
                  )}
                  onClick={() => onChange(ChoiceManager.goTo(state, index))}
                  disabled={disabled}
                  aria-label={`第 ${index + 1} 题`}
                  aria-current={selected ? "step" : undefined}
                  data-testid={`choice-step-${index}`}
                >
                  {valid ? <Check className="h-3.5 w-3.5" /> : index + 1}
                </button>
              );
            })}
          </div>
        </div>

        <div className="mt-3 rounded-md border border-border/60 bg-background/50 p-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="text-xs font-medium text-primary">
                {state.activeIndex + 1} / {state.request.questions.length}
              </div>
              <h3 className="mt-1 text-sm font-semibold text-foreground">
                {question.title}
              </h3>
              {question.description ? (
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  {question.description}
                </p>
              ) : null}
            </div>
          </div>

          <div className="mt-3 grid gap-2">
            {options.map((option) => {
              const selected = answer.optionId === option.id;
              const isCustom = option.id === CUSTOM_CHOICE_OPTION_ID;
              return (
                <button
                  key={option.id}
                  type="button"
                  className={cn(
                    "w-full rounded-md border p-3 text-left transition-colors",
                    selected
                      ? "border-primary bg-primary/10 text-foreground"
                      : "border-border/70 bg-card/75 text-foreground hover:border-primary/45 hover:bg-primary/5",
                  )}
                  onClick={() =>
                    onChange(ChoiceManager.select(state, question.id, option.id))
                  }
                  disabled={disabled}
                  aria-pressed={selected}
                  data-testid={`choice-option-${option.id}`}
                >
                  <div className="flex items-start gap-2">
                    <span
                      className={cn(
                        "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
                        selected
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-muted-foreground/40",
                      )}
                    >
                      {selected ? <Check className="h-3 w-3" /> : null}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5 text-sm font-medium">
                        {isCustom ? <PencilLine className="h-3.5 w-3.5" /> : null}
                        {option.label}
                      </span>
                      <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                        {option.description}
                      </span>
                    </span>
                  </div>
                </button>
              );
            })}
          </div>

          {answer.optionId === CUSTOM_CHOICE_OPTION_ID ? (
            <Textarea
              className="mt-2 min-h-20 resize-none bg-card/80 text-sm"
              value={answer.customText}
              onChange={(event) =>
                onChange(
                  ChoiceManager.setCustomText(
                    state,
                    question.id,
                    event.target.value,
                  ),
                )
              }
              disabled={disabled}
              placeholder="输入你的选择"
              aria-label="自定义选择"
              data-testid="choice-custom-text"
            />
          ) : null}
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <div className="text-xs text-muted-foreground">
            {canSubmit ? "已完成全部选择" : currentValid ? "当前问题已选择" : "请选择当前问题"}
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={disabled || isFirst}
              onClick={() => onChange(ChoiceManager.previous(state))}
              data-testid="choice-prev"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              上一题
            </Button>
            {!isLast ? (
              <Button
                type="button"
                size="sm"
                disabled={disabled || !currentValid}
                onClick={() => onChange(ChoiceManager.next(state))}
                data-testid="choice-next"
              >
                下一题
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            ) : (
              <Button
                type="button"
                size="sm"
                disabled={disabled || !canSubmit}
                onClick={submit}
                data-testid="choice-confirm"
              >
                <Check className="h-3.5 w-3.5" />
                确定
              </Button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
