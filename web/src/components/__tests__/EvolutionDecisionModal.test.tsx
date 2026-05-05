import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EvolutionDecisionModal } from "@/components/EvolutionDecisionModal";
import * as apiMod from "@/lib/api";
import type { EvolutionReviewDTO } from "@/protocol";

function buildReview(
  overrides: Partial<EvolutionReviewDTO> = {},
): EvolutionReviewDTO {
  return {
    review_id: "evo-review:run-1",
    run_id: "run-1",
    session_id: "thread-1",
    reviewed_at_ms: 100,
    review_summary: "captured nutrients",
    nutrients: [
      {
        nutrient_id: "n1",
        kind: "workflow",
        title: "Skill Nutrient",
        content: "turn repeated manual recovery into skill",
        summary: "skill summary",
        confidence: 0.95,
        evidence_turns: [3],
        source_run_id: "run-1",
        source_session_id: "thread-1",
        suggested_target: "skill",
        tags: [],
      },
      {
        nutrient_id: "n2",
        kind: "memory",
        title: "Memory Nutrient",
        content: "remember to pin decision contract first",
        summary: "memory summary",
        confidence: 0.82,
        evidence_turns: [5],
        source_run_id: "run-1",
        source_session_id: "thread-1",
        suggested_target: "memory",
        tags: [],
      },
    ],
    decision_summary: {
      total: 2,
      accepted_memory: 1,
      accepted_skill: 1,
      ignored: 0,
      pending: 0,
    },
    decisions: [
      {
        nutrient_id: "n1",
        decision: "accept_skill",
        target: "skill",
        decided_at_ms: 110,
        applied_status: "written",
        applied_mode: "create",
        applied_path: "/workspace/.kongming/skills/focus-skill/SKILL.md",
        applied_at_ms: 120,
        applied_error: null,
      },
      {
        nutrient_id: "n2",
        decision: "accept_memory",
        target: "memory",
        decided_at_ms: 111,
        applied_status: "failed",
        applied_mode: "append",
        applied_path: "/workspace/.kongming/memory/MEMORY.md",
        applied_at_ms: 121,
        applied_error: "write conflict",
      },
    ],
    ...overrides,
  };
}

describe("EvolutionDecisionModal", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("refresh 回放后展示 applied 状态、mode、路径和错误", async () => {
    vi.spyOn(apiMod, "apiGetEvolutionReviews").mockResolvedValue([buildReview()]);

    render(
      <EvolutionDecisionModal
        open
        onOpenChange={() => undefined}
        threadId="thread-1"
        reviewId="evo-review:run-1"
        onReviewUpdated={() => undefined}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Skill Nutrient")).toBeInTheDocument();
    });

    expect(screen.getByText("已写入")).toBeInTheDocument();
    expect(screen.getByText("create")).toBeInTheDocument();
    expect(
      screen.getByText(".../.kongming/skills/focus-skill/SKILL.md"),
    ).toBeInTheDocument();
    expect(screen.getByText("写入失败")).toBeInTheDocument();
    expect(screen.getByText("append")).toBeInTheDocument();
    expect(screen.getByText("write conflict")).toBeInTheDocument();
  });

  it("提交 decision 后用返回的 review 更新 apply 回显", async () => {
    const pendingReview = buildReview({
      decision_summary: {
        total: 2,
        accepted_memory: 0,
        accepted_skill: 0,
        ignored: 0,
        pending: 2,
      },
      decisions: [],
    });
    const updatedReview = buildReview({
      decision_summary: {
        total: 2,
        accepted_memory: 1,
        accepted_skill: 0,
        ignored: 0,
        pending: 1,
      },
      decisions: [
        {
          nutrient_id: "n2",
          decision: "accept_memory",
          target: "memory",
          decided_at_ms: 222,
          applied_status: "written",
          applied_mode: "append",
          applied_path: "/workspace/.kongming/memory/MEMORY.md",
          applied_at_ms: 223,
          applied_error: null,
        },
      ],
    });
    const onReviewUpdated = vi.fn();

    vi.spyOn(apiMod, "apiGetEvolutionReviews").mockResolvedValue([pendingReview]);
    vi.spyOn(apiMod, "apiPostEvolutionDecision").mockResolvedValue({
      review: updatedReview,
    });

    render(
      <EvolutionDecisionModal
        open
        onOpenChange={() => undefined}
        threadId="thread-1"
        reviewId="evo-review:run-1"
        onReviewUpdated={onReviewUpdated}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Memory Nutrient")).toBeInTheDocument();
    });

    fireEvent.click(screen.getAllByText("采纳为记忆")[1]!);

    await waitFor(() => {
      expect(screen.getByText("已写入")).toBeInTheDocument();
      expect(screen.getByText("append")).toBeInTheDocument();
      expect(screen.getByText(".../.kongming/memory/MEMORY.md")).toBeInTheDocument();
    });

    const memoryButtons = screen.getAllByRole("button", { name: "采纳为记忆" });
    const skillButtons = screen.getAllByRole("button", { name: "采纳为技能" });
    const ignoreButtons = screen.getAllByRole("button", { name: "忽略" });
    expect(memoryButtons[1]).toHaveAttribute("aria-pressed", "true");
    expect(skillButtons[1]).toHaveAttribute("aria-pressed", "false");
    expect(ignoreButtons[1]).toHaveAttribute("aria-pressed", "false");

    expect(onReviewUpdated).toHaveBeenCalledWith(updatedReview);
  });

  it("已忽略条目会把忽略按钮切到选中态", async () => {
    vi.spyOn(apiMod, "apiGetEvolutionReviews").mockResolvedValue([
      buildReview({
        decisions: [
          {
            nutrient_id: "n1",
            decision: "ignore",
            target: null,
            decided_at_ms: 111,
            applied_status: "skipped",
            applied_mode: "ignore",
            applied_path: null,
            applied_at_ms: 111,
            applied_error: null,
          },
        ],
      }),
    ]);

    render(
      <EvolutionDecisionModal
        open
        onOpenChange={() => undefined}
        threadId="thread-1"
        reviewId="evo-review:run-1"
        onReviewUpdated={() => undefined}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Skill Nutrient")).toBeInTheDocument();
    });

    const memoryButtons = screen.getAllByRole("button", { name: "采纳为记忆" });
    const skillButtons = screen.getAllByRole("button", { name: "采纳为技能" });
    const ignoreButtons = screen.getAllByRole("button", { name: "忽略" });
    expect(memoryButtons[0]).toHaveAttribute("aria-pressed", "false");
    expect(skillButtons[0]).toHaveAttribute("aria-pressed", "false");
    expect(ignoreButtons[0]).toHaveAttribute("aria-pressed", "true");
  });

  it("存在 pending 条目时显示补跑入口，并用补跑结果刷新 review", async () => {
    const pendingReview = buildReview({
      decision_summary: {
        total: 2,
        accepted_memory: 1,
        accepted_skill: 1,
        ignored: 0,
        pending: 0,
      },
      decisions: [
        {
          nutrient_id: "n1",
          decision: "accept_skill",
          target: "skill",
          decided_at_ms: 110,
          applied_status: "pending",
          applied_mode: "create",
          applied_path: null,
          applied_at_ms: null,
          applied_error: null,
        },
      ],
    });
    const updatedReview = buildReview({
      decisions: [
        {
          nutrient_id: "n1",
          decision: "accept_skill",
          target: "skill",
          decided_at_ms: 110,
          applied_status: "written",
          applied_mode: "create",
          applied_path: "/workspace/.kongming/skills/focus-skill/SKILL.md",
          applied_at_ms: 130,
          applied_error: null,
        },
      ],
    });
    const onReviewUpdated = vi.fn();

    vi.spyOn(apiMod, "apiGetEvolutionReviews").mockResolvedValue([pendingReview]);
    vi.spyOn(apiMod, "apiPostEvolutionReapply").mockResolvedValue({
      review: updatedReview,
    });

    render(
      <EvolutionDecisionModal
        open
        onOpenChange={() => undefined}
        threadId="thread-1"
        reviewId="evo-review:run-1"
        onReviewUpdated={onReviewUpdated}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("补跑待写入")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("补跑待写入"));

    await waitFor(() => {
      expect(screen.getByText("已写入")).toBeInTheDocument();
      expect(screen.queryByText("补跑待写入")).not.toBeInTheDocument();
    });

    expect(onReviewUpdated).toHaveBeenCalledWith(updatedReview);
  });
});
