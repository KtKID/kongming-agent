import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGetThreadSubAgents } from "@/lib/api";
import type {
  ThreadSubAgentDTO,
  ThreadSubAgentDisplayItem,
  ThreadSubAgentIconVariant,
} from "@/protocol";

interface UseThreadSubAgentsOptions {
  enabled: boolean;
  refreshMs?: number;
}

const ACTIVE_STATUSES = new Set([
  "active",
  "in_progress",
  "running",
  "started",
  "spawning",
]);
const SUCCESS_STATUSES = new Set(["completed", "done", "success", "succeeded"]);
const ERROR_STATUSES = new Set(["error", "failed", "failure"]);
const CANCELLED_STATUSES = new Set(["canceled", "cancelled"]);
const PENDING_STATUSES = new Set(["pending", "queued", "waiting"]);
const MAX_SUBAGENTS = 128;
const MAX_SUBAGENT_NAME_LENGTH = 120;

function normalizeStatus(status: string): string {
  return status.trim().toLowerCase();
}

function subAgentStatusMeta(status: string): {
  label: string;
  icon: ThreadSubAgentIconVariant;
  isActive: boolean;
} {
  const normalized = normalizeStatus(status);
  if (ACTIVE_STATUSES.has(normalized)) {
    return { label: "运行中", icon: "running", isActive: true };
  }
  if (SUCCESS_STATUSES.has(normalized)) {
    return { label: "已完成", icon: "success", isActive: false };
  }
  if (ERROR_STATUSES.has(normalized)) {
    return { label: "失败", icon: "error", isActive: false };
  }
  if (CANCELLED_STATUSES.has(normalized)) {
    return { label: "已取消", icon: "error", isActive: false };
  }
  if (PENDING_STATUSES.has(normalized)) {
    return { label: "等待中", icon: "pending", isActive: false };
  }
  return {
    label: status.trim() || "未知",
    icon: "pending",
    isActive: false,
  };
}

function subAgentName(agent: ThreadSubAgentDTO): string {
  return (
    agent.task_name?.trim() ||
    agent.name?.trim() ||
    agent.id?.trim() ||
    "子智能体"
  ).slice(0, MAX_SUBAGENT_NAME_LENGTH);
}

function subAgentSourceLabel(agent: ThreadSubAgentDTO): string | undefined {
  const workflowId = agent.workflow_id?.trim();
  if (workflowId) return workflowId;
  const source = agent.source?.trim();
  return source || undefined;
}

export function toThreadSubAgentDisplayItems(
  subagents: ThreadSubAgentDTO[],
): ThreadSubAgentDisplayItem[] {
  return subagents.slice(0, MAX_SUBAGENTS).map((agent, index) => {
    const name = subAgentName(agent);
    const meta = subAgentStatusMeta(agent.status);
    const sourceLabel = subAgentSourceLabel(agent);
    const key = [
      agent.id?.trim(),
      sourceLabel,
      name,
      agent.started_at_ms ?? "",
      agent.updated_at_ms ?? "",
      index,
    ]
      .filter(Boolean)
      .join(":");
    return {
      key,
      name,
      status: agent.status,
      status_label: meta.label,
      icon_variant: meta.icon,
      is_active: meta.isActive,
      source_label: sourceLabel,
      started_at_ms: agent.started_at_ms,
      updated_at_ms: agent.updated_at_ms,
      aria_label: `${meta.label}：${name}`,
    };
  });
}

export function useThreadSubAgents(
  threadId: string | undefined,
  { enabled, refreshMs = 2000 }: UseThreadSubAgentsOptions,
) {
  const [items, setItems] = useState<ThreadSubAgentDisplayItem[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const fetchSubAgents = useCallback(async () => {
    if (!threadId || !enabled) return;
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setIsLoading(true);
    setError(null);
    try {
      const nextSubAgents = await apiGetThreadSubAgents(threadId);
      if (requestSeq.current === seq) {
        setItems(toThreadSubAgentDisplayItems(nextSubAgents));
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

    void fetchSubAgents();
    const timer = window.setInterval(() => {
      void fetchSubAgents();
    }, refreshMs);

    return () => {
      requestSeq.current += 1;
      window.clearInterval(timer);
    };
  }, [enabled, fetchSubAgents, refreshMs, threadId]);

  return useMemo(
    () => ({
      items,
      isLoading,
      error,
      refresh: fetchSubAgents,
    }),
    [error, fetchSubAgents, isLoading, items],
  );
}
