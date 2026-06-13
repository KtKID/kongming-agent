// kongming-agent v0.1.5 ESLint 配置（flat config）
//
// - typescript-eslint 推荐 + react-hooks
// - 不开 too strict 的规则；happy path 优先
// - 忽略 dist / node_modules / shadcn ui 复制实现 / e2e 用例（联调阶段单独跑）
//
// network-layer v0.1：
// - 业务层禁止 import `@/network/tools`（tools 是 network 包私有工具）
// - network 内部解除该限制
// - flat config 后置覆盖前置，所以"解除限制"的对象必须放在"限制"的对象之后

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
  // network-layer v0.1：业务层禁 @/network/tools
  {
    files: ["src/**/*.{ts,tsx}", "tests/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["@/network/tools", "@/network/tools/*"],
              message:
                "@/network/tools 是 network 包私有工具，只允许 @/network/ 内部 import",
            },
          ],
        },
      ],
    },
  },
  // network-layer v0.1：network 包内部解除限制
  {
    files: ["src/network/**/*.{ts,tsx}"],
    rules: { "no-restricted-imports": "off" },
  },
);
