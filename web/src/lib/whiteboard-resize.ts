import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";

/**
 * 白板面板宽度持久化 + 拖拽 hook。
 *
 * 仅服务于 desktop 档（>=1080px / `!compactMode && !mobileMode`）。
 * compact / mobile / 折叠态由调用方传 `enabled=false` 关闭拖拽与手柄渲染，
 * 但 hook 自身始终维护一个 width state（无害冗余，避免 enabled 切换抖动）。
 *
 * 持久化：localStorage key `kongming.whiteboard.width`，写入失败静默吞错
 * （隐私模式 / 配额满 / 不可写都不应阻塞 UI）。
 *
 * 边界：min 384px、max `viewport - 320px`（chat 区至少保留 320px）。
 */

export const WHITEBOARD_WIDTH_KEY = "kongming.whiteboard.width";
export const WHITEBOARD_DEFAULT_WIDTH = 800;
export const WHITEBOARD_MIN_WIDTH = 384;
export const WHITEBOARD_RESERVED_CHAT_WIDTH = 320;

export function getMaxWidth(viewportWidth: number): number {
  // viewportWidth NaN / Infinity 防御：浏览器规范下 window.innerWidth 必为有限数值，
  // 此分支仅为 API 公开调用的对称防御（与 clampWidth 的 NaN 处理对齐）
  if (!Number.isFinite(viewportWidth)) return WHITEBOARD_MIN_WIDTH;
  const candidate = viewportWidth - WHITEBOARD_RESERVED_CHAT_WIDTH;
  return Math.max(WHITEBOARD_MIN_WIDTH, candidate);
}

export function clampWidth(raw: number, viewportWidth: number): number {
  if (!Number.isFinite(raw)) return WHITEBOARD_DEFAULT_WIDTH;
  const max = getMaxWidth(viewportWidth);
  const rounded = Math.round(raw);
  if (rounded < WHITEBOARD_MIN_WIDTH) return WHITEBOARD_MIN_WIDTH;
  if (rounded > max) return max;
  return rounded;
}

export function loadPersistedWidth(viewportWidth: number): number {
  if (typeof window === "undefined") return WHITEBOARD_DEFAULT_WIDTH;
  try {
    const raw = window.localStorage.getItem(WHITEBOARD_WIDTH_KEY);
    if (raw == null) return clampWidth(WHITEBOARD_DEFAULT_WIDTH, viewportWidth);
    const parsed = Number.parseFloat(raw);
    return clampWidth(parsed, viewportWidth);
  } catch {
    return clampWidth(WHITEBOARD_DEFAULT_WIDTH, viewportWidth);
  }
}

export function persistWidth(width: number): void {
  if (typeof window === "undefined") return;
  // NaN / Infinity 防御：不让脏数据进 localStorage（与 clampWidth 的 NaN 处理对齐）
  if (!Number.isFinite(width)) return;
  try {
    window.localStorage.setItem(WHITEBOARD_WIDTH_KEY, String(Math.round(width)));
  } catch {
    // 隐私模式 / 配额满 / 不可写：吞错，不阻塞 UI
  }
}

export interface UseWhiteboardResizeOptions {
  /**
   * 是否启用拖拽。typical: `isOpen && !compactMode && !mobileMode`。
   * - enabled=false 时 startResize / resetWidth 变 no-op
   * - hook 始终维护 width state 避免切档抖动
   */
  enabled: boolean;
}

export interface UseWhiteboardResizeResult {
  width: number;
  isResizing: boolean;
  startResize: (event: React.PointerEvent) => void;
  resetWidth: () => void;
}

function readViewportWidth(): number {
  if (typeof window === "undefined") return 1440;
  return window.innerWidth;
}

export function useWhiteboardResize(
  options: UseWhiteboardResizeOptions,
): UseWhiteboardResizeResult {
  const { enabled } = options;

  const [width, setWidth] = useState<number>(() =>
    loadPersistedWidth(readViewportWidth()),
  );
  const [isResizing, setIsResizing] = useState(false);

  // ref 持拖拽起点，避免 pointermove 闭包里读 stale width
  const dragStateRef = useRef<{
    pointerId: number;
    startClientX: number;
    startWidth: number;
  } | null>(null);

  // enabled 切到 false 时如正在拖拽则强制结束：
  // 场景——用户在 desktop 档（>=1080px）按下手柄正在拖拽时，视窗缩到 compact/mobile
  // 触发父组件传 enabled=false。此时组件层不再渲染 handle，但 hook 内部 isResizing 仍
  // 为 true，全局 pointermove 监听还在改 width。强制清状态保证语义一致 + 持久化当前宽度。
  useEffect(() => {
    if (enabled || !isResizing) return;
    dragStateRef.current = null;
    setIsResizing(false);
    // 用 setWidth callback 拿最新 width 持久化，避免把 width 加进 deps 触发多余跑
    setWidth((current) => {
      persistWidth(current);
      return current;
    });
  }, [enabled, isResizing]);

  // 视窗 resize 时把 width clamp 到新边界（屏幕变小不能让白板溢出）
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onResize = () => {
      setWidth((current) => {
        const clamped = clampWidth(current, window.innerWidth);
        if (clamped !== current) {
          persistWidth(clamped);
        }
        return clamped;
      });
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // 仅在拖拽过程中绑全局 pointermove / pointerup，结束即解绑
  useEffect(() => {
    if (!isResizing) return;

    const onPointerMove = (event: PointerEvent) => {
      const drag = dragStateRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      // 白板在视窗右侧，从左边缘拖动：鼠标向左移 → width 增加
      const deltaX = drag.startClientX - event.clientX;
      const nextRaw = drag.startWidth + deltaX;
      setWidth(clampWidth(nextRaw, readViewportWidth()));
    };

    const finalize = (event: PointerEvent) => {
      const drag = dragStateRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      dragStateRef.current = null;
      setIsResizing(false);
      // 用最新 state 持久化
      setWidth((current) => {
        persistWidth(current);
        return current;
      });
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", finalize);
    window.addEventListener("pointercancel", finalize);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", finalize);
      window.removeEventListener("pointercancel", finalize);
    };
  }, [isResizing]);

  const startResize = useCallback(
    (event: React.PointerEvent) => {
      if (!enabled) return;
      event.preventDefault();
      event.stopPropagation();
      dragStateRef.current = {
        pointerId: event.pointerId,
        startClientX: event.clientX,
        startWidth: width,
      };
      setIsResizing(true);
    },
    [enabled, width],
  );

  const resetWidth = useCallback(() => {
    if (!enabled) return;
    const next = clampWidth(WHITEBOARD_DEFAULT_WIDTH, readViewportWidth());
    setWidth(next);
    persistWidth(next);
  }, [enabled]);

  return { width, isResizing, startResize, resetWidth };
}
