import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGetThreadTaskProgress } from "@/lib/api";
import type {
  ThreadTaskProgressDisplayItem,
  ThreadTaskProgressIconVariant,
  ThreadTaskProgressSnapshot,
  ThreadTaskProgressStatus,
  ThreadTaskProgressViewModel,
} from "@/protocol";

const STATUS_META: Record<
  ThreadTaskProgressStatus,
  {
    label: ThreadTaskProgressDisplayItem["status_label"];
    icon: ThreadTaskProgressIconVariant;
  }
> = {
  pending: { label: "未完成", icon: "ring" },
  in_progress: { label: "进行中", icon: "active_ring" },
  completed: { label: "已完成", icon: "check_circle" },
};
const MAX_PROGRESS_ITEMS = 128;
const MAX_PROGRESS_DESC_LENGTH = 1000;

export function toThreadTaskProgressViewModel(
  snapshot?: ThreadTaskProgressSnapshot | null,
): ThreadTaskProgressViewModel {
  const items = [...(snapshot?.tasks ?? [])]
    .sort((a, b) => a.display_order - b.display_order)
    .slice(0, MAX_PROGRESS_ITEMS)
    .map<ThreadTaskProgressDisplayItem>((item) => {
      const meta = STATUS_META[item.status];
      const desc = item.desc.slice(0, MAX_PROGRESS_DESC_LENGTH);
      return {
        key: item.orchestration_task_id,
        orchestration_task_id: item.orchestration_task_id,
        task_id: item.task_id,
        desc,
        status: item.status,
        status_label: meta.label,
        icon_variant: meta.icon,
        order: item.display_order,
        aria_label: `${meta.label}：${desc}`,
      };
    });

  return {
    title: "进度",
    variant: "compact_checklist",
    items,
    empty: {
      title: "暂无任务进度",
      desc: "当前 thread 还没有可展示的 checklist。",
    },
  };
}

interface UseThreadTaskProgressOptions {
  enabled: boolean;
  refreshMs?: number;
}

export function useThreadTaskProgress(
  threadId: string | undefined,
  { enabled, refreshMs = 2000 }: UseThreadTaskProgressOptions,
) {
  const [snapshot, setSnapshot] = useState<ThreadTaskProgressSnapshot | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const fetchProgress = useCallback(async () => {
    if (!threadId || !enabled) return;
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setIsLoading(true);
    setError(null);
    try {
      const nextSnapshot = await apiGetThreadTaskProgress(threadId);
      if (requestSeq.current === seq) {
        setSnapshot(nextSnapshot);
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
      setSnapshot(null);
      return;
    }

    void fetchProgress();
    const timer = window.setInterval(() => {
      void fetchProgress();
    }, refreshMs);

    return () => {
      requestSeq.current += 1;
      window.clearInterval(timer);
    };
  }, [enabled, fetchProgress, refreshMs, threadId]);

  const viewModel = useMemo(
    () => toThreadTaskProgressViewModel(snapshot),
    [snapshot],
  );

  return {
    snapshot,
    viewModel,
    isLoading,
    error,
    refresh: fetchProgress,
  };
}
