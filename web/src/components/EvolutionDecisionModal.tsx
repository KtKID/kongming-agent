import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  apiGetEvolutionReviews,
  apiPostEvolutionDecision,
  apiPostEvolutionReapply,
  ApiError,
  RateLimitedError,
} from "@/lib/api";
import type {
  EvolutionDecisionItemDTO,
  EvolutionDecisionValue,
  EvolutionReviewDTO,
} from "@/protocol";

export function EvolutionDecisionModal({
  open,
  onOpenChange,
  threadId,
  reviewId,
  onReviewUpdated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  threadId: string;
  reviewId: string;
  onReviewUpdated: (review: EvolutionReviewDTO) => void;
}) {
  const [review, setReview] = useState<EvolutionReviewDTO | null>(null);
  const [loading, setLoading] = useState(false);
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  const [reapplying, setReapplying] = useState(false);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    apiGetEvolutionReviews(threadId)
      .then((reviews) => {
        if (!active) return;
        const matched = reviews.find((item) => item.review_id === reviewId) ?? null;
        setReview(matched);
      })
      .catch((error: unknown) => {
        if (!active) return;
        toast.error(errorMessage(error));
      })
      .finally(() => {
        if (!active) return;
        setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open, reviewId, threadId]);

  async function handleDecision(
    nutrientId: string,
    decision: EvolutionDecisionValue,
  ) {
    setSubmittingId(nutrientId);
    try {
      const response = await apiPostEvolutionDecision(threadId, reviewId, {
        nutrient_id: nutrientId,
        decision,
      });
      setReview(response.review);
      onReviewUpdated(response.review);
    } catch (error: unknown) {
      toast.error(errorMessage(error));
    } finally {
      setSubmittingId(null);
    }
  }

  async function handleReapply() {
    setReapplying(true);
    try {
      const response = await apiPostEvolutionReapply(threadId, reviewId);
      setReview(response.review);
      onReviewUpdated(response.review);
    } catch (error: unknown) {
      toast.error(errorMessage(error));
    } finally {
      setReapplying(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>处理进化养料</DialogTitle>
          <DialogDescription>
            {review
              ? `已处理 ${handledCount(review)}/${review.decision_summary.total} 条`
              : "读取复盘结果中"}
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="text-sm text-muted-foreground">正在加载进化养料...</div>
        ) : null}

        {!loading && !review ? (
          <div className="rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
            未找到对应复盘记录
          </div>
        ) : null}

        {review ? (
          <div className="space-y-4">
            {needsReapply(review) ? (
              <div className="flex items-center justify-between gap-3 rounded-xl border border-border bg-muted/30 px-4 py-3 text-sm">
                <div className="text-muted-foreground">
                  检测到历史待写入或失败条目，批量补跑后会同步更新记忆和技能落盘结果。
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={reapplying}
                  onClick={handleReapply}
                >
                  {reapplying ? "补跑中" : "补跑待写入"}
                </Button>
              </div>
            ) : null}
            <div className="rounded-xl border border-border bg-muted/30 px-4 py-3 text-sm">
              <div className="font-medium">复盘摘要</div>
              <div className="mt-1 text-muted-foreground">{review.review_summary}</div>
            </div>

            {review.nutrients.map((nutrient) => {
              const decision = review.decisions.find(
                (item) => item.nutrient_id === nutrient.nutrient_id,
              );
              const applyState = decision ? applyStateLabel(decision) : null;
              const applyMode = decision?.applied_mode
                ? applyModeLabel(decision.applied_mode)
                : null;
              const applyPath = decision?.applied_path
                ? shortenPath(decision.applied_path)
                : null;
              return (
                <div
                  key={nutrient.nutrient_id}
                  className="rounded-2xl border border-border bg-card px-4 py-4 shadow-sm"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="text-sm font-semibold">{nutrient.title}</div>
                    <Badge variant="secondary">{nutrient.kind}</Badge>
                    <Badge variant="outline">
                      confidence {nutrient.confidence.toFixed(2)}
                    </Badge>
                    {decision ? (
                      <Badge variant="success">{decisionLabel(decision.decision)}</Badge>
                    ) : (
                      <Badge variant="warning">待处理</Badge>
                    )}
                    {decision && applyState ? (
                      <Badge variant={applyBadgeVariant(decision)}>{applyState}</Badge>
                    ) : null}
                    {applyMode ? <Badge variant="outline">{applyMode}</Badge> : null}
                  </div>
                  <div className="mt-3 space-y-2 text-sm">
                    <div className="text-foreground/90">{nutrient.content}</div>
                    <div className="text-xs text-muted-foreground">
                      evidence_turns: {nutrient.evidence_turns.join(", ")}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      suggested_target: {nutrient.suggested_target ?? "n/a"}
                    </div>
                    {applyPath ? (
                      <div className="text-xs text-muted-foreground">
                        写入路径: <code>{applyPath}</code>
                      </div>
                    ) : null}
                    {decision?.applied_error ? (
                      <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                        {decision.applied_error}
                      </div>
                    ) : null}
                  </div>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      variant={decisionButtonVariant(decision?.decision, "accept_memory")}
                      aria-pressed={decision?.decision === "accept_memory"}
                      disabled={submittingId === nutrient.nutrient_id}
                      onClick={() => handleDecision(nutrient.nutrient_id, "accept_memory")}
                    >
                      采纳为记忆
                    </Button>
                    <Button
                      size="sm"
                      variant={decisionButtonVariant(decision?.decision, "accept_skill")}
                      aria-pressed={decision?.decision === "accept_skill"}
                      disabled={submittingId === nutrient.nutrient_id}
                      onClick={() => handleDecision(nutrient.nutrient_id, "accept_skill")}
                    >
                      采纳为技能
                    </Button>
                    <Button
                      size="sm"
                      variant={decisionButtonVariant(decision?.decision, "ignore")}
                      aria-pressed={decision?.decision === "ignore"}
                      disabled={submittingId === nutrient.nutrient_id}
                      onClick={() => handleDecision(nutrient.nutrient_id, "ignore")}
                    >
                      忽略
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function needsReapply(review: EvolutionReviewDTO): boolean {
  return review.decisions.some(
    (decision) =>
      decision.decision !== "ignore" &&
      (decision.applied_status === "pending" || decision.applied_status === "failed"),
  );
}

function handledCount(review: EvolutionReviewDTO): number {
  return (
    review.decision_summary.accepted_memory +
    review.decision_summary.accepted_skill +
    review.decision_summary.ignored
  );
}

function decisionLabel(decision: EvolutionDecisionValue): string {
  switch (decision) {
    case "accept_memory":
      return "已采纳到记忆";
    case "accept_skill":
      return "已采纳到技能";
    case "ignore":
      return "已忽略";
    default: {
      const _exhaustive: never = decision;
      return _exhaustive;
    }
  }
}

function decisionButtonVariant(
  current: EvolutionDecisionValue | null | undefined,
  expected: EvolutionDecisionValue,
): "default" | "outline" | "secondary" {
  if (current === expected) {
    return "default";
  }
  if (expected === "ignore") {
    return "secondary";
  }
  return "outline";
}

function applyStateLabel(decision: EvolutionDecisionItemDTO): string | null {
  switch (decision.applied_status) {
    case "pending":
      return "写入中";
    case "written":
      return "已写入";
    case "skipped":
      return "已命中";
    case "failed":
      return "写入失败";
    case null:
    case undefined:
      return null;
    default: {
      const _exhaustive: never = decision.applied_status;
      return _exhaustive;
    }
  }
}

function applyModeLabel(mode: NonNullable<EvolutionDecisionItemDTO["applied_mode"]>): string {
  switch (mode) {
    case "append":
      return "append";
    case "update":
      return "update";
    case "create":
      return "create";
    case "ignore":
      return "ignore";
    default: {
      const _exhaustive: never = mode;
      return _exhaustive;
    }
  }
}

function applyBadgeVariant(
  decision: EvolutionDecisionItemDTO,
): "secondary" | "success" | "warning" | "destructive" {
  switch (decision.applied_status) {
    case "pending":
      return "secondary";
    case "written":
      return "success";
    case "skipped":
      return "warning";
    case "failed":
      return "destructive";
    case null:
    case undefined:
      return "secondary";
    default: {
      const _exhaustive: never = decision.applied_status;
      return _exhaustive;
    }
  }
}

function shortenPath(path: string): string {
  const kongmingIndex = path.indexOf("/.kongming/");
  if (kongmingIndex >= 0) {
    return `...${path.slice(kongmingIndex)}`;
  }
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= 4) {
    return path;
  }
  return `.../${parts.slice(-4).join("/")}`;
}

function errorMessage(error: unknown): string {
  if (error instanceof RateLimitedError) {
    return `请求过于频繁，请 ${error.retryAfterSeconds}s 后重试`;
  }
  if (error instanceof ApiError) {
    return error.detail;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}
