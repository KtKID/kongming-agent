/**
 * 接入 /ws/cron 事件并更新 store
 *
 * v0.2 加固（reports/cr/cr-report-20260519-web-crash-investigation.md P1 #2）：
 * 加 auth gate——只在用户已登录且 auth check 完成时才发起 WS 连接。
 * 之前未登录页面也会触发 connectCronWS → 鉴权失败 → 死循环重连风暴。
 *
 * v0.6 死代码清理：移除 `cron.run.failed` / `cron.task.updated` 两类分支——
 * 后端 `cron_delivery.py` 全仓只 broadcast `cron.run.completed` 一种事件
 * （失败状态通过帧内 `status` 字段承载，不是单独 event kind）。
 */

import { useEffect } from "react";
import { useAuthStore } from "@/stores/auth";
import { useSchedulerStore } from "../store";
import { connectCronWS, disconnectCronWS } from "../ws";

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
      if (msg.frame_type === "cron.run.completed") {
        // 运行完成（含失败 status）→ 刷新任务列表以更新 lastRunAt / state
        void refreshTasks();
      }
    });
    return () => {
      disconnectCronWS();
    };
  }, [authChecked, authenticated, refreshTasks]);
}
