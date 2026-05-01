import type { Config } from "tailwindcss";
import {
  borderRadius,
  duration,
  easing,
  fontFamily,
  fontSize,
  shadow,
  spacing,
} from "./src/lib/design-tokens";

/**
 * kongming-agent v0.1.5 Tailwind 配置
 *
 * - shadcn/ui new-york 风格的颜色映射（HSL CSS var）
 * - design-tokens.ts 注入 spacing / typography / motion / shadow
 * - darkMode 走 media（prefers-color-scheme）；不引入手动切换 UI
 * - content 扫描 src + components/ui（shadcn 后续 pull 进来）
 */

export default {
  darkMode: ["class"],
  content: [
    "./index.html",
    "./src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
        },
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(var(--success-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      borderRadius: {
        sm: borderRadius.sm,
        DEFAULT: borderRadius.DEFAULT,
        md: borderRadius.md,
        lg: borderRadius.lg,
        xl: borderRadius.xl,
      },
      spacing: spacing as Record<string, string>,
      fontFamily: {
        sans: fontFamily.sans as unknown as string[],
        mono: fontFamily.mono as unknown as string[],
      },
      fontSize: fontSize as unknown as Record<
        string,
        [string, { lineHeight: string }]
      >,
      boxShadow: shadow,
      transitionDuration: {
        fast: `${duration.fast}ms`,
        DEFAULT: `${duration.normal}ms`,
        slow: `${duration.slow}ms`,
      },
      transitionTimingFunction: {
        standard: easing.standard,
        enter: easing.enter,
        exit: easing.exit,
        spring: easing.spring,
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "slide-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "fade-in": "fade-in 200ms cubic-bezier(0, 0, 0.2, 1)",
        "slide-up": "slide-up 200ms cubic-bezier(0, 0, 0.2, 1)",
      },
    },
  },
  plugins: [
    // tailwindcss-animate 提供 shadcn 组件需要的 animate-in / fade-in / slide-* 动效
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    require("tailwindcss-animate"),
  ],
} satisfies Config;
