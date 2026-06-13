import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchAgentWorkflows } from "@/modules/agent-workflow-viewer/api";
import type { WorkflowListItemDTO } from "@/modules/agent-workflow-viewer/types";

export interface ThreadWorkflowHistoryItem {
  workflow_id: string;
  title: string;
  status: string;
  status_label: string;
  started_at: string | null;
  finished_at: string | null;
}

interface UseThreadWorkflowHistoryOptions {
  enabled: boolean;
  refreshMs?: number;
}

const STATUS_LABELS: Record<string, string> = {
  completed: "已完成",
  failed: "失败",
  running: "进行中",
  pending: "未完成",
  unknown: "未知",
};

export function toThreadWorkflowHistoryItems(
  workflows: WorkflowListItemDTO[],
): ThreadWorkflowHistoryItem[] {
  return workflows.map((workflow) => {
    const normalizedStatus = workflow.status.trim().toLowerCase() || "unknown";
    return {
      workflow_id: workflow.workflow_id,
      title: workflow.title.trim() || workflow.workflow_id,
      status: workflow.status,
      status_label: STATUS_LABELS[normalizedStatus] ?? workflow.status,
      started_at: workflow.started_at,
      finished_at: workflow.finished_at,
    };
  });
}

export function useThreadWorkflowHistory(
  threadId: string | undefined,
  { enabled, refreshMs = 5000 }: UseThreadWorkflowHistoryOptions,
) {
  const [items, setItems] = useState<ThreadWorkflowHistoryItem[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const fetchHistory = useCallback(async () => {
    if (!threadId || !enabled) return;
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setIsLoading(true);
    setError(null);
    try {
      const list = await fetchAgentWorkflows(threadId);
      if (requestSeq.current === seq) {
        setItems(toThreadWorkflowHistoryItems(list.workflows));
      }
    } catch (err) {
      if (requestSeq.current === seq) {
        const message = err instanceof Error ? err.message : String(err);
        setError(message);
      }
    } finally {
      if (requestSeq.current === seq) {
        setIsLoading(false);
      }
    }
  }, [enabled, threadId]);

  useEffect(() => {
    if (!enabled || !threadId) {
      requestSeq.current += 1;
      setIsLoading(false);
      setError(null);
      setItems([]);
      return;
    }

    void fetchHistory();
    const timer = window.setInterval(() => {
      void fetchHistory();
    }, refreshMs);

    return () => {
      requestSeq.current += 1;
      window.clearInterval(timer);
    };
  }, [enabled, fetchHistory, refreshMs, threadId]);

  return useMemo(
    () => ({
      items,
      isLoading,
      error,
      refresh: fetchHistory,
    }),
    [error, fetchHistory, isLoading, items],
  );
}
