/**
 * Chat 页面布局断点判定。
 *
 * 真源：旧版 Chat.tsx inline 逻辑（44b9c28 之前）
 *   isMobile  = width < 768
 *   isCompact = width < 1080
 *   compact 时初始关闭白板与侧边栏
 *
 * 提取到独立 module 便于：
 *   - SSR fallback 复用同一份阈值
 *   - 单元测试覆盖断点边界
 *
 * 该文件被 44b9c28 漏 git add 导致缺失，于本次 pull 后补回。
 */

export const MOBILE_BREAKPOINT = 768;
export const COMPACT_BREAKPOINT = 1080;

export interface ChatLayoutState {
  /** 移动端布局（< 768px）：侧边栏与白板互斥展开 */
  isMobileLayout: boolean;
  /** 紧凑布局（< 1080px）：默认折叠侧边栏 / 白板 */
  isCompactLayout: boolean;
  /** 初始或 resize 后是否应当展开白板 */
  shouldOpenWhiteboard: boolean;
}

export function getChatLayoutState(width: number): ChatLayoutState {
  const isMobileLayout = width < MOBILE_BREAKPOINT;
  const isCompactLayout = width < COMPACT_BREAKPOINT;
  return {
    isMobileLayout,
    isCompactLayout,
    shouldOpenWhiteboard: !isCompactLayout,
  };
}
