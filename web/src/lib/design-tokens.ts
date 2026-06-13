/**
 * kongming-agent v0.1.5 设计 token（spacing / typography / motion / shadow）
 *
 * 这里集中放可被 Tailwind config import 的纯 JS 常量；
 * 颜色 token 用 CSS variables（在 globals.css 里），便于跟随系统 dark/light 自动切换。
 *
 * 所有数值取 8px 网格 + Apple HIG 动效曲线启发；不直接抄 SF Symbols（图标用 lucide-react）。
 */

export const fontFamily = {
  sans: [
    "Avenir Next",
    "SF Pro Display",
    "-apple-system",
    "BlinkMacSystemFont",
    "PingFang SC",
    "Helvetica Neue",
    "sans-serif",
  ],
  mono: [
    "JetBrains Mono",
    "SF Mono",
    "Berkeley Mono",
    "ui-monospace",
    "Menlo",
    "monospace",
  ],
} as const;

/** 8px 网格 spacing 扩展（Tailwind 默认已有 0/0.5/1/...，这里只加补充值） */
export const spacing = {
  "9": "2.25rem", // 36
  "11": "2.75rem", // 44
  "13": "3.25rem", // 52
  "15": "3.75rem", // 60
  "18": "4.5rem", // 72
} as const;

/** Apple HIG 启发的 easing 曲线 */
export const easing = {
  standard: "cubic-bezier(0.4, 0, 0.2, 1)",
  enter: "cubic-bezier(0, 0, 0.2, 1)",
  exit: "cubic-bezier(0.4, 0, 1, 1)",
  spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
} as const;

/** 动效时长（ms） */
export const duration = {
  fast: 120,
  normal: 200,
  slow: 320,
} as const;

/** 阴影层级（玻璃质感 + 微下沉） */
export const shadow = {
  sm: "0 8px 20px -16px hsl(236 35% 2% / 0.85)",
  md: "0 18px 42px -24px hsl(236 35% 2% / 0.72)",
  lg: "0 32px 80px -38px hsl(236 35% 2% / 0.82)",
  glass:
    "0 28px 72px -40px hsl(236 35% 2% / 0.9), inset 0 1px 0 0 hsl(0 0% 100% / 0.08), inset 0 0 0 1px hsl(257 92% 72% / 0.06)",
} as const;

/** 字体尺寸（与 line-height 配对） */
export const fontSize = {
  xs: ["0.75rem", { lineHeight: "1rem" }],
  sm: ["0.875rem", { lineHeight: "1.25rem" }],
  base: ["0.9375rem", { lineHeight: "1.5rem" }], // 15px 是阅读最舒适密度
  lg: ["1.0625rem", { lineHeight: "1.625rem" }],
  xl: ["1.25rem", { lineHeight: "1.75rem" }],
  "2xl": ["1.5rem", { lineHeight: "2rem" }],
  "3xl": ["2rem", { lineHeight: "2.5rem" }],
} as const;

/** 圆角层级 */
export const borderRadius = {
  sm: "0.25rem",
  DEFAULT: "0.5rem",
  md: "0.625rem", // shadcn new-york 默认
  lg: "1rem",
  xl: "1.25rem",
} as const;
