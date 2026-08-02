import type { ComponentType, ReactNode } from "react";

export type WebShellRailScope = "global" | "thread";

export type WebShellRailDensity = "desktop" | "compact" | "mobile";

export type WebShellRailOpenSource = "hover" | "focus" | "keyboard" | null;

export type WebShellRailPriority = "p0" | "p1" | "p2";

export type WebShellHostEnvironment = "browser" | "xspace";

export interface WebShellCapabilities {
  xspaceHost: boolean;
  nativeFileDialog: boolean;
}

export type WebShellCapabilityKey = keyof WebShellCapabilities;

export interface WebShellRailContext {
  /** 当前布局密度，来源 useChatLayout / chat-layout 判定 */
  density: WebShellRailDensity;
  /** 当前线程 ID，缺省时线程级入口隐藏 */
  activeThreadId?: string;
  /** 当前线程标题，用于 tooltip、aria-label 和测试断言 */
  activeThreadTitle?: string;
  /** 是否存在可用当前线程，决定 thread scope 工具可用性 */
  hasActiveThread: boolean;
  /** 当前 Web 会话登录状态，决定 logout 等全局入口可用性 */
  isAuthenticated: boolean;
  /** 当前 Web sidecar 宿主环境，来源客户端配置 */
  hostEnvironment: WebShellHostEnvironment;
  /** 当前 Web sidecar 可用宿主能力集合 */
  capabilities: WebShellCapabilities;
}

export interface WebShellRailRenderProps {
  /** rail 统一按钮 className，保证固定尺寸和视觉密度 */
  className: string;
  /** rail 统一图标 className，保证图标尺寸稳定 */
  iconClassName: string;
  /** 工具项标签，用于 aria-label 或 title */
  label: string;
  /** 入口触发后关闭 rail */
  closeRail: () => void;
}

export interface WebShellRailItem {
  /** 工具项稳定 ID，用于排序、测试选择器和状态追踪 */
  id: string;
  /** 工具项作用域，决定全局入口或线程入口 */
  scope: WebShellRailScope;
  /** 展示优先级，影响 compact 模式裁剪 */
  priority: WebShellRailPriority;
  /** 现有 UI 文案标签，用于 aria-label 和 tooltip */
  label: string;
  /** lucide 图标组件，默认渲染入口使用 */
  icon: ComponentType<{ className?: string }>;
  /** 当前上下文下是否可展示 */
  available: boolean;
  /** 禁用原因，用于 tooltip 和测试断言 */
  disabledReason?: string;
  /** 工具项所需宿主能力，缺省表示普通浏览器环境也可展示 */
  requiredCapability?: WebShellCapabilityKey;
  /** 普通链接入口，存在时默认渲染为 Link */
  to?: string;
  /** 普通动作入口，存在时默认渲染为 button */
  onSelect?: () => void | Promise<void>;
  /** 复用复杂现有入口能力的渲染工厂 */
  render?: (props: WebShellRailRenderProps) => ReactNode;
}

export interface WebShellRailState {
  /** rail 当前展开状态 */
  open: boolean;
  /** 最近一次展开来源 */
  openedBy: WebShellRailOpenSource;
  /** 当前响应式密度 */
  density: WebShellRailDensity;
  /** 当前可见工具项列表 */
  visibleItems: WebShellRailItem[];
}

export interface WebShellRailLayoutConfig {
  /** 可见 rail 宽度，单位 px */
  railWidthPx: number;
  /** 最左侧 hover 触发区宽度，单位 px */
  hoverZoneWidthPx: number;
  /** hover 触发区高度，单位 px，必须大于按钮 stack 高度参考值 */
  hoverZoneHeightPx: number;
  /** rail 按钮边长，单位 px */
  buttonSizePx: number;
  /** rail 按钮间距，单位 px */
  buttonGapPx: number;
  /** rail 层级，高于 LeftSidebar，低于全局弹窗与 Toast */
  zIndex: number;
  /** 展开态覆盖 LeftSidebar 区域，固定为 true */
  overlayLeftSidebar: true;
}
