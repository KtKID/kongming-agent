/**
 * dashboard/config 状态层（ConfigPage 的 zustand store）。
 *
 * ## 职责
 *
 * - 持有 schema / effective / raw 三段 fetch 结果
 * - 维护编辑差量（`dirty: {path: value}`），按路径增量记录用户改动
 * - 串联 save / restart 链路（含 30s health polling）
 * - **纯状态 + actions**，不做 React 副作用，不调 window.* / document.*
 *   （reload 判断由 UI 层订阅 `restartStatus === "ready"` 自行决定）
 *
 * ## 模块边界
 *
 * sibling 组件（components/sections/ConfigPage）**不允许**直接 import
 * `./api`，必须经过本 store 操作数据。这样 fetch 调用 + 错误归一化
 * 只在一处发生，UI 层只看 store 字段。
 *
 * ## 写法约定
 *
 * 沿用项目现有 zustand 写法（参考 `@/modules/dashboard/store.ts` 和
 * `@/stores/threadStatus.ts`）：`create<State>((set, get) => ({...}))`
 * 单实例 + 字段平铺，不上 `subscribeWithSelector` / slice 分片。
 */

import { create } from "zustand";

import { ApiError } from "@/lib/api";
import {
  getEffective,
  getHealth,
  getRaw,
  getSchema,
  restart as apiRestart,
  savePatch,
} from "./api";
import type {
  EffectiveResponse,
  PatchItem,
  RawResponse,
  SchemaResponse,
  ValidationError,
} from "./types";

export type LoadStatus = "idle" | "loading" | "loaded" | "error";

export type SaveStatus =
  | "idle"
  | "saving"
  | "saved"
  | "error"
  | "conflict"
  | "validation_error";

export type RestartStatus =
  | "idle"
  | "restarting"
  | "polling_health"
  | "ready"
  | "timeout";

/** 健康检查轮询间隔（ms）。 */
const HEALTH_POLL_INTERVAL_MS = 1000;
/** 健康检查最大尝试次数（30 次 × 1s = 30s 总超时）。 */
const HEALTH_POLL_MAX_ATTEMPTS = 30;

interface ConfigState {
  // 加载态
  schema: SchemaResponse | null;
  effective: EffectiveResponse | null;
  raw: RawResponse | null;
  loadStatus: LoadStatus;
  loadError: string | null;

  // 编辑态（差量保存）
  /** {path: 新值}，只存改过的字段；与 effective.values[path] 相等的会被自动清除。 */
  dirty: Record<string, unknown>;
  /** 等价于 Object.keys(dirty).length，仅供 UI 直接订阅避免重算。 */
  dirtyCount: number;

  // 保存态
  saveStatus: SaveStatus;
  validationErrors: ValidationError[];
  saveError: string | null;
  /** 上次 save 返回的 restart_required_fields。 */
  pendingRestartFields: string[];

  // 重启态
  restartStatus: RestartStatus;
  restartError: string | null;

  // actions
  load: () => Promise<void>;
  setField: (path: string, value: unknown) => void;
  clearField: (path: string) => void;
  save: () => Promise<void>;
  restart: () => Promise<void>;
  reset: () => void;
}

/**
 * 健康轮询的取消句柄。保存在模块作用域，`reset()` / 新一次 `restart()`
 * 都能强行 invalidate 上一轮（防 stale poll 写回过期 status）。
 *
 * 不用 AbortController：fetch 已经在 `apiGet` 内部走 ApiError 异常路径，
 * 取消只需在每次循环 tick 检查 token 是否还有效即可，逻辑更简单。
 */
let currentPollToken = 0;

/** 严格相等比较，兼顾对象/数组场景（dirty 清理判断）。 */
function valuesEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a === null || b === null) return false;
  if (typeof a !== typeof b) return false;
  if (typeof a !== "object") return false;
  // 对 list / dict 用 JSON 序列化比较；config 字段都是简单 plain 结构，可接受
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.detail || err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export const useConfigStore = create<ConfigState>((set, get) => ({
  schema: null,
  effective: null,
  raw: null,
  loadStatus: "idle",
  loadError: null,

  dirty: {},
  dirtyCount: 0,

  saveStatus: "idle",
  validationErrors: [],
  saveError: null,
  pendingRestartFields: [],

  restartStatus: "idle",
  restartError: null,

  load: async () => {
    set({ loadStatus: "loading", loadError: null });
    try {
      const [schema, effective, raw] = await Promise.all([
        getSchema(),
        getEffective(),
        getRaw(),
      ]);
      set({
        schema,
        effective,
        raw,
        loadStatus: "loaded",
        loadError: null,
      });
    } catch (err) {
      set({
        loadStatus: "error",
        loadError: describeError(err),
      });
    }
  },

  setField: (path, value) => {
    const { effective, dirty } = get();
    const baseline = effective?.values[path];

    // 若新值与 effective baseline 相等，从 dirty 删除（撤销编辑）
    if (effective && valuesEqual(value, baseline)) {
      if (!(path in dirty)) return;
      const next = { ...dirty };
      delete next[path];
      set({ dirty: next, dirtyCount: Object.keys(next).length });
      return;
    }

    // 同值再次写入，跳过 set（防止无意义 re-render）
    if (path in dirty && valuesEqual(dirty[path], value)) return;

    const next = { ...dirty, [path]: value };
    set({ dirty: next, dirtyCount: Object.keys(next).length });
  },

  clearField: (path) => {
    const { dirty } = get();
    if (!(path in dirty)) return;
    const next = { ...dirty };
    delete next[path];
    set({ dirty: next, dirtyCount: Object.keys(next).length });
  },

  save: async () => {
    const { dirty, raw } = get();
    if (Object.keys(dirty).length === 0 || !raw) {
      // 无改动或未加载，静默忽略（UI 层应禁用按钮）
      return;
    }

    set({
      saveStatus: "saving",
      validationErrors: [],
      saveError: null,
    });

    const patch: PatchItem[] = Object.entries(dirty).map(([path, value]) => ({
      path,
      value,
    }));

    try {
      const resp = await savePatch({
        patch,
        expected_mtime: raw.mtime,
      });
      set({
        saveStatus: "saved",
        dirty: {},
        dirtyCount: 0,
        raw: { ...raw, mtime: resp.new_mtime },
        pendingRestartFields: resp.restart_required_fields,
        validationErrors: [],
        saveError: null,
      });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 422) {
          // ValidationFailedError：后端会把 ValidationError[] 放在 details.errors
          const detailErrors = err.details?.errors;
          const errors: ValidationError[] = Array.isArray(detailErrors)
            ? (detailErrors as ValidationError[])
            : [];
          set({
            saveStatus: "validation_error",
            validationErrors: errors,
            saveError: err.detail || err.message,
          });
          return;
        }
        if (err.status === 409) {
          set({
            saveStatus: "conflict",
            saveError: err.detail || err.message,
          });
          return;
        }
      }
      set({
        saveStatus: "error",
        saveError: describeError(err),
      });
    }
  },

  restart: async () => {
    // 每次 restart 申请一个新 token，旧轮询 tick 检查到 token 变了自动退出
    currentPollToken += 1;
    const myToken = currentPollToken;

    set({
      restartStatus: "restarting",
      restartError: null,
    });

    try {
      await apiRestart();
    } catch (err) {
      // restart 触发失败：进程没起来就别浪费 30s 轮询
      set({
        restartStatus: "timeout",
        restartError: describeError(err),
      });
      return;
    }

    if (get().restartStatus !== "restarting" || myToken !== currentPollToken) {
      // 被 reset() / 新一轮 restart 中断
      return;
    }

    set({ restartStatus: "polling_health" });

    for (let attempt = 0; attempt < HEALTH_POLL_MAX_ATTEMPTS; attempt += 1) {
      await sleep(HEALTH_POLL_INTERVAL_MS);
      if (myToken !== currentPollToken) {
        // 取消信号：直接退出，不写回状态
        return;
      }
      try {
        await getHealth();
        if (myToken !== currentPollToken) return;
        set({
          restartStatus: "ready",
          restartError: null,
        });
        return;
      } catch {
        // 重启过程中预期会失败：503 / 网络中断 / fetch reject 都正常，继续轮询
      }
    }

    if (myToken !== currentPollToken) return;
    set({
      restartStatus: "timeout",
      restartError: `健康检查 ${HEALTH_POLL_MAX_ATTEMPTS}s 超时未恢复`,
    });
  },

  reset: () => {
    // invalidate 任何在跑的健康轮询
    currentPollToken += 1;
    set({
      schema: null,
      effective: null,
      raw: null,
      loadStatus: "idle",
      loadError: null,

      dirty: {},
      dirtyCount: 0,

      saveStatus: "idle",
      validationErrors: [],
      saveError: null,
      pendingRestartFields: [],

      restartStatus: "idle",
      restartError: null,
    });
  },
}));
