/**
 * Sitian 模块 REST 客户端（internal）
 *
 * 只暴露 fetchSitianReport，外部组件禁止直接 import 本文件，
 * 应通过 `hooks/useSitian` -> manager 路径访问。
 *
 * 后端字段已为 camelCase（保持与 sitian_report.json 原样），
 * 因此不做 DTO → VM 映射，直接 cast。
 */

import { ApiError, apiGet } from "@/lib/api";
import type { SitianReport } from "./types";

/**
 * 报告文件不存在（后端 404 `{"error":"no_report"}`）的语义化错误。
 *
 * UI 用 `instanceof SitianNoReportError` 区分"没生成过"与"网络/服务错误"两种空态。
 */
export class SitianNoReportError extends Error {
  constructor(message = "尚未生成司天报告") {
    super(message);
    this.name = "SitianNoReportError";
  }
}

/**
 * 拉取最新司天报告。
 *
 * - 200 → SitianReport
 * - 404 → 抛 SitianNoReportError
 * - 5xx / 网络错误 → 抛原 Error / ApiError
 */
export async function fetchSitianReport(): Promise<SitianReport> {
  try {
    return await apiGet<SitianReport>("/api/sitian/report");
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      throw new SitianNoReportError();
    }
    throw err;
  }
}
