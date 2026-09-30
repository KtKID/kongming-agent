/**
 * track-timeline-v0.1 · 轨迹时间线 store（zustand，per-thread 状态隔离）。
 *
 * 数据（TrajectoryResponse wire DTO）由 TrajectoryManager 拉取后写入；
 * 本 store 只持有状态：数据 / 加载 / 错误（404 与网络错误分开）/ 选中记录 /
 * 搜索词 / 轮次折叠 / 工具调用展开。选中与折叠状态独立于搜索过滤存在，
 * 清空搜索后原样恢复（spec feat05 场景3）。
 */

import { create } from "zustand";
import type { TrajectoryResponse } from "@/protocol";
import type { TurnGroupKey } from "./derive";

/** 加载失败分类：404（会话不存在）与其它（网络 / 服务端错误）分开渲染。 */
export type TrajectoryLoadError = "not_found" | "network";

interface TrajectoryState {
  dataByThread: Record<string, TrajectoryResponse | undefined>;
  loadingByThread: Record<string, boolean | undefined>;
  errorByThread: Record<string, TrajectoryLoadError | undefined>;
  selectedSeqByThread: Record<string, number | undefined>;
  searchByThread: Record<string, string>;
  collapsedTurnsByThread: Record<string, string[] | undefined>;
  toolsCollapsedByThread: Record<string, boolean | undefined>;

  /** Manager 写入接口：开始 / 成功 / 失败。 */
  beginLoad: (threadId: string) => void;
  finishLoad: (threadId: string, data: TrajectoryResponse) => void;
  failLoad: (threadId: string, error: TrajectoryLoadError) => void;

  /** 选中一条记录（seq）；undefined 取消选中。 */
  select: (threadId: string, seq: number | undefined) => void;
  setSearch: (threadId: string, query: string) => void;
  toggleTurnCollapsed: (threadId: string, key: TurnGroupKey) => void;
  setAllTurnsCollapsed: (threadId: string, collapsed: boolean) => void;
  /** 「调用」一键展开 / 收起工具调用明细（收起时轮内只显示计数摘要行）。 */
  setToolsCollapsed: (threadId: string, collapsed: boolean) => void;
  resetThread: (threadId: string) => void;
}

export const useTrajectoryStore = create<TrajectoryState>((set) => ({
  dataByThread: {},
  loadingByThread: {},
  errorByThread: {},
  selectedSeqByThread: {},
  searchByThread: {},
  collapsedTurnsByThread: {},
  toolsCollapsedByThread: {},

  beginLoad: (threadId) =>
    set((state) => ({
      loadingByThread: { ...state.loadingByThread, [threadId]: true },
      errorByThread: { ...state.errorByThread, [threadId]: undefined },
    })),

  finishLoad: (threadId, data) =>
    set((state) => ({
      dataByThread: { ...state.dataByThread, [threadId]: data },
      loadingByThread: { ...state.loadingByThread, [threadId]: false },
      errorByThread: { ...state.errorByThread, [threadId]: undefined },
    })),

  failLoad: (threadId, error) =>
    set((state) => ({
      loadingByThread: { ...state.loadingByThread, [threadId]: false },
      errorByThread: { ...state.errorByThread, [threadId]: error },
    })),

  select: (threadId, seq) =>
    set((state) => ({
      selectedSeqByThread: { ...state.selectedSeqByThread, [threadId]: seq },
    })),

  setSearch: (threadId, query) =>
    set((state) => ({
      searchByThread: { ...state.searchByThread, [threadId]: query },
    })),

  toggleTurnCollapsed: (threadId, key) =>
    set((state) => {
      const current = new Set(state.collapsedTurnsByThread[threadId] ?? []);
      if (current.has(key)) {
        current.delete(key);
      } else {
        current.add(key);
      }
      return {
        collapsedTurnsByThread: {
          ...state.collapsedTurnsByThread,
          [threadId]: [...current],
        },
      };
    }),

  setAllTurnsCollapsed: (threadId, collapsed) =>
    set((state) => {
      let keys: string[] | undefined;
      if (collapsed) {
        // 折叠全部：从当前数据派生全部分组键
        const data = state.dataByThread[threadId];
        keys = data
          ? [...new Set(data.records.map((r) => `${r.run_index}:${r.turn ?? 0}`))]
          : [];
      } else {
        keys = [];
      }
      return {
        collapsedTurnsByThread: {
          ...state.collapsedTurnsByThread,
          [threadId]: keys,
        },
      };
    }),

  setToolsCollapsed: (threadId, collapsed) =>
    set((state) => ({
      toolsCollapsedByThread: {
        ...state.toolsCollapsedByThread,
        [threadId]: collapsed,
      },
    })),

  resetThread: (threadId) =>
    set((state) => {
      // 逐 map 删除该 thread 的键（而非写 undefined），保持值类型不含 undefined
      const strip = <T>(map: Record<string, T>): Record<string, T> => {
        const next = { ...map };
        delete next[threadId];
        return next;
      };
      return {
        dataByThread: strip(state.dataByThread),
        loadingByThread: strip(state.loadingByThread),
        errorByThread: strip(state.errorByThread),
        selectedSeqByThread: strip(state.selectedSeqByThread),
        searchByThread: strip(state.searchByThread),
        collapsedTurnsByThread: strip(state.collapsedTurnsByThread),
        toolsCollapsedByThread: strip(state.toolsCollapsedByThread),
      };
    }),
}));
