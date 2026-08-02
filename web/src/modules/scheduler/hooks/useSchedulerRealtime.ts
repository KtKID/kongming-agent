/**
 * 接入 /ws/cron 事件并更新 store
 *
 * v0.2 加固（reports/cr/cr-report-20260519-web-crash-investigation.md P1 #2）：
 * 加 auth gate——只在用户已登录且 auth check 完成时才发起 WS 连接。
 * 之前未登录页面也会触发 connectCronWS → 鉴权失败 → 死循环重连风暴。
 *
 * `cron.run.completed` 表示投递消息已产生，可能早于 durable terminal CAS；
 * 面板 live 状态只由 Manager 在清理 live 索引后发出的 `cron.run.finished` 收口。
 */

import { useEffect } from "react";
import { useAuthStore } from "@/stores/auth";
import { useSchedulerStore } from "../store";
import { connectCronWS, disconnectCronWS } from "../ws";

function getMessageTaskId(msg: { task_id: string }): string {
  return msg.task_id;
}

function getMessageRunId(msg: { run_id: string }): string {
  return msg.run_id;
}

export function useSchedulerRealtime() {
  const refreshTasks = useSchedulerStore((s) => s.refreshTasks);
  const authenticated = useAuthStore((s) => s.authenticated);
  const authChecked = useAuthStore((s) => s._checked);

  useEffect(() => {
    // auth gate：必须 check 完成 + 已登录 才连
    // _checked === false：还在初始 GET /api/auth/me，等
    // authenticated === false：未登录或 cookie 失效，不连（避免无谓 1008）
    if (!authChecked || !authenticated) return;

    connectCronWS((msg) => {
      if (msg.frame_type === "cron.run.started") {
        const taskId = getMessageTaskId(msg);
        const {
          selectedTaskId,
          selectedRunIdByTaskId,
          pendingManualRunTaskId,
          loadRuns,
          markRunStarted,
          selectRun,
          markPendingManualRun,
        } =
          useSchedulerStore.getState();
        const runId = getMessageRunId(msg);
        if (taskId && runId) markRunStarted(taskId, runId);
        const shouldFollowManualRun = Boolean(
          taskId && pendingManualRunTaskId === taskId,
        );
        if (
          taskId &&
          runId &&
          (!selectedRunIdByTaskId[taskId] || shouldFollowManualRun)
        ) {
          selectRun(taskId, runId);
        }
        if (shouldFollowManualRun) {
          markPendingManualRun(null);
        }
        if (selectedTaskId && (!taskId || selectedTaskId === taskId)) {
          void loadRuns(selectedTaskId);
        }
        return;
      }

      if (msg.frame_type === "cron.run.completed") {
        return;
      }

      if (msg.frame_type === "cron.run.finished") {
        // 运行完成（含失败 status）→ 刷新任务列表以更新 lastRunAt / state
        void refreshTasks();
        const taskId = getMessageTaskId(msg);
        const {
          selectedTaskId,
          selectedRunIdByTaskId,
          pendingManualRunTaskId,
          loadRuns,
          markRunFinished,
          selectRun,
          markPendingManualRun,
        } =
          useSchedulerStore.getState();
        const runId = getMessageRunId(msg);
        if (taskId && runId) markRunFinished(taskId, runId);
        if (taskId && pendingManualRunTaskId === taskId) {
          markPendingManualRun(null);
        }
        if (taskId && runId && !selectedRunIdByTaskId[taskId]) {
          selectRun(taskId, runId);
        }
        if (selectedTaskId && (!taskId || selectedTaskId === taskId)) {
          void loadRuns(selectedTaskId);
        }
      }
    });
    return () => {
      disconnectCronWS();
    };
  }, [authChecked, authenticated, refreshTasks]);
}
