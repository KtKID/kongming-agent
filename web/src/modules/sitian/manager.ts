/**
 * SitianManager（internal）
 *
 * 模块**唯一**副作用入口：fetch、状态切换都收口在这里。
 * 组件只能通过 `hooks/useSitian` 间接调 manager，禁止直接 import 本文件或 store / api。
 *
 * 设计动机（task README #3）：
 *   - 组件保持纯渲染，所有 fetch / open / close 走单一 manager 调用
 *   - 测试时直接 spy manager 即可，不必 mock 全套 zustand + fetch
 */

import { ApiError } from "@/lib/api";
import { fetchSitianReport, SitianNoReportError } from "./api";
import { useSitianStore } from "./store";

export interface SitianManager {
  /** 打开弹窗 + 拉取最新报告。任何错误吃掉并写入 store.error / store.noReport。 */
  open: () => Promise<void>;
  /** 关闭弹窗 + 清空 report / error 等中间态。 */
  close: () => void;
  /** 重新拉取报告，不改 isOpen（用于"刷新"按钮）。 */
  refresh: () => Promise<void>;
}

function loadReport(): Promise<void> {
  const store = useSitianStore.getState();
  store.setLoading(true);
  store.setError(null);
  store.setNoReport(false);

  return fetchSitianReport()
    .then((report) => {
      useSitianStore.getState().setReport(report);
    })
    .catch((err: unknown) => {
      const next = useSitianStore.getState();
      next.setReport(null);
      if (err instanceof SitianNoReportError) {
        next.setNoReport(true);
        return;
      }
      if (err instanceof ApiError) {
        next.setError(`司天报告获取失败：${err.detail || err.message}`);
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      next.setError(`司天报告获取失败：${msg}`);
    })
    .finally(() => {
      useSitianStore.getState().setLoading(false);
    });
}

function createSitianManager(): SitianManager {
  return {
    async open() {
      const store = useSitianStore.getState();
      store.setOpen(true);
      await loadReport();
    },
    close() {
      // 完整重置：保留 isOpen=false 之外其余字段一律清空
      useSitianStore.getState().reset();
    },
    async refresh() {
      await loadReport();
    },
  };
}

export const sitianManager: SitianManager = createSitianManager();
