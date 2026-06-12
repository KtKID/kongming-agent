/**
 * dashboard/config 子模块的**唯一对外 fetch 出口**。
 *
 * sibling 模块（components/store/page）**不允许跨过本文件**直接 fetch
 * `/api/manage/config/*` 或 `/api/health`。所有调用必须走这里的函数，
 * 这样 CSRF 头、401 拦截、错误码归一化只在 `@/lib/api` 一处发生。
 *
 * 重启场景说明（getHealth）：重启过程中端点可能返回 503 / 网络中断 /
 * fetch reject。本层让错误**正常 throw**（apiGet 内部已包装成 `ApiError`），
 * 由 store 层负责 catch + 退避重试，不在本文件做 retry 兜底（避免双层重试
 * 互相干扰，也避免静默吞错）。
 */

import { apiGet, apiPost } from "@/lib/api";
import type {
  EffectiveResponse,
  HealthResponse,
  RawResponse,
  RestartResponse,
  SavePatchRequest,
  SavePatchResponse,
  SchemaResponse,
} from "./types";

export function getSchema(): Promise<SchemaResponse> {
  return apiGet<SchemaResponse>("/api/manage/config/schema");
}

export function getEffective(): Promise<EffectiveResponse> {
  return apiGet<EffectiveResponse>("/api/manage/config/effective");
}

export function getRaw(): Promise<RawResponse> {
  return apiGet<RawResponse>("/api/manage/config/raw");
}

export function savePatch(
  req: SavePatchRequest,
): Promise<SavePatchResponse> {
  return apiPost<SavePatchResponse>("/api/manage/config/save", req);
}

export function restart(): Promise<RestartResponse> {
  return apiPost<RestartResponse>("/api/manage/config/restart", {});
}

/**
 * 健康检查（公开端点，无 auth）。重启后轮询用。
 *
 * 失败语义全部走 `ApiError` throw（含 status=0 的 network error），caller 自行
 * 判断是否进入"重启中"分支并退避重试。
 */
export function getHealth(): Promise<HealthResponse> {
  return apiGet<HealthResponse>("/api/health");
}
