/**
 * Sitian 模块 Zustand Store（internal）
 *
 * 只持有模块状态，所有副作用（fetch、refresh）由 `manager.ts` 编排。
 * 组件禁止直接 import 本文件，应通过 `hooks/useSitian` 访问。
 */

import { create } from "zustand";
import type { SitianReport } from "./types";

export interface SitianStoreState {
  isOpen: boolean;
  loading: boolean;
  error: string | null;
  /** null 表示尚未拉取或刚 reset；与"报告不存在"（error 为空字符串区分）。 */
  report: SitianReport | null;
  /** 报告不存在（后端 404）时为 true，与一般错误区分以便 UI 友好显示。 */
  noReport: boolean;
}

export interface SitianStoreActions {
  setOpen: (open: boolean) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
  setReport: (report: SitianReport | null) => void;
  setNoReport: (noReport: boolean) => void;
  reset: () => void;
}

const INITIAL_STATE: SitianStoreState = {
  isOpen: false,
  loading: false,
  error: null,
  report: null,
  noReport: false,
};

export const useSitianStore = create<SitianStoreState & SitianStoreActions>(
  (set) => ({
    ...INITIAL_STATE,

    setOpen: (open) => set({ isOpen: open }),
    setLoading: (loading) => set({ loading }),
    setError: (error) => set({ error }),
    setReport: (report) => set({ report }),
    setNoReport: (noReport) => set({ noReport }),

    reset: () => set({ ...INITIAL_STATE }),
  }),
);
