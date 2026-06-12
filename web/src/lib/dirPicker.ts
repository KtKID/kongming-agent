/**
 * 双轨目录选择 helper（v0.1：projects-registry）
 *
 * - **Tauri 环境**（webview，`window.__TAURI_INTERNALS__` 存在）→ 通过
 *   `@tauri-apps/plugin-dialog` 调起原生文件夹选择器，返回绝对路径。
 * - **浏览器环境**（普通 browser，没有 webview 桥）→ 返回 `null`，调用方应触发
 *   fallback UI（手动输入框 + "📍 一键填当前 worktree" 按钮）。
 *
 * 设计要点：
 * 1. `@tauri-apps/plugin-dialog` 走 **dynamic import**，避免浏览器场景同步加载
 *    Tauri runtime（保留打包干净 + 避免无意义的运行时警告）。
 * 2. `isTauriEnv()` 提前快速过滤；非 webview 环境绝不会进入 dynamic import 分支。
 * 3. fallback dialog 用到的 `_REPO_ROOT` 由 server 的 `/api/server/info` 提供
 *    （#9 已落地 `ServerInfoResponse`）。
 */

import { apiGet } from "@/lib/api";
import type { ServerInfoResponse } from "@/protocol";

/**
 * 是否在 Tauri WebView 环境内。
 *
 * 检测 `window.__TAURI_INTERNALS__`：Tauri v2 webview 注入的私有桥对象。
 * SSR / Node 环境（无 `window`）也安全返回 `false`。
 */
export function isTauriEnv(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/**
 * 双轨目录选择。
 *
 * - Tauri webview → 调原生 dialog，用户选定返绝对路径，取消返 `null`。
 * - 浏览器 → 直接返 `null`，让调用方自己渲染 fallback 输入框。
 */
export async function pickDirectory(opts?: {
  title?: string;
  defaultPath?: string;
}): Promise<string | null> {
  if (!isTauriEnv()) {
    return null;
  }

  // dynamic import 避免浏览器场景同步拉 Tauri runtime
  const { open } = await import("@tauri-apps/plugin-dialog");
  const result = await open({
    directory: true,
    multiple: false,
    title: opts?.title ?? "选择项目目录",
    defaultPath: opts?.defaultPath,
  });

  // open() 取消时返回 null；选定单目录时返回绝对路径字符串。
  // multiple:false 不会返 string[]，但 TS 类型仍需窄化以排除该可能。
  return typeof result === "string" ? result : null;
}

/**
 * 拉取 server 端 `_REPO_ROOT`（web server 进程项目根的绝对路径）。
 *
 * 用于 fallback dialog 的 "📍 一键填当前 worktree" 按钮：浏览器场景没法调原生
 * dialog，至少能让用户一键填上"当前 server 跑的目录"作为快速起点。
 */
export async function getServerRepoRoot(): Promise<string> {
  const data = await apiGet<ServerInfoResponse>("/api/server/info");
  return data.repo_root;
}
