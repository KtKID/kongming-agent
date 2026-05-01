/**
 * kongming-agent v0.1.5 PostCSS 配置
 *
 * Tailwind 3 走 postcss + autoprefixer 标准链路。
 * vite 自动加载本文件，不需要在 vite.config.ts 显式声明。
 */
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
