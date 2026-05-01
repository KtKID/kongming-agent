// kongming-agent v0.1.5 ESLint 配置（flat config）
//
// - typescript-eslint 推荐 + react-hooks
// - 不开 too strict 的规则；happy path 优先
// - 忽略 dist / node_modules / shadcn ui 复制实现 / e2e 用例（联调阶段单独跑）

import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";

export default tseslint.config(
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "playwright-report/**",
      "test-results/**",
      // shadcn 组件按官方实现保留；不在我们的 lint 范围
      "src/components/ui/**",
    ],
  },
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}", "tests/**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/no-explicit-any": "warn",
      // 允许 `e: unknown` catch 上的 instanceof 模式
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },
);
