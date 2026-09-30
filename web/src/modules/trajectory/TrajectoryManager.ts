/**
 * track-timeline-v0.1 · TrajectoryManager（轨迹模块门户）。
 *
 * 职责：拉取 `GET /api/threads/{id}/trajectory` 并把结果写入
 * TrajectoryStore（404 与网络错误分类落 store）。组件只通过本门户取数，
 * 不直接触碰 api 层与 store 写接口（对齐 AGENTS 前端门户约定）。
 *
 * v0.1 为手动刷新（spec feat01 场景4）：Panel 挂载时 load 一次，
 * 用户点「刷新」再 load。
 */

import { apiGet, ApiError } from "@/lib/api";
import type { TrajectoryResponse } from "@/protocol";
import { useTrajectoryStore } from "./TrajectoryStore";

export class TrajectoryManager {
  /** 拉取并落 store；404 → not_found，其它错误 → network。 */
  async load(threadId: string): Promise<void> {
    const store = useTrajectoryStore.getState();
    store.beginLoad(threadId);
    try {
      const dto = await apiGet<TrajectoryResponse>(
        `/api/threads/${threadId}/trajectory`,
      );
      useTrajectoryStore.getState().finishLoad(threadId, dto);
    } catch (error) {
      const kind =
        error instanceof ApiError && error.status === 404
          ? ("not_found" as const)
          : ("network" as const);
      useTrajectoryStore.getState().failLoad(threadId, kind);
    }
  }
}

/** 模块级单例（组件经 hooks 消费 store，经此门户触发加载）。 */
export const trajectoryManager = new TrajectoryManager();
