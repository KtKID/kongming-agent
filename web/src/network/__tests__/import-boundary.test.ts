/**
 * Import 边界测试：`@/network/tools` 只允许在 `web/src/network/` 内部 import。
 *
 * 这是给 eslint `no-restricted-imports` 规则做兜底——eslint 跑过一次后，未来
 * 误添加 import 时 vitest 也能捕获（CI 不一定单跑 eslint）。
 */

import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";

const WEB_SRC = path.resolve(__dirname, "../../");

/**
 * 递归收集 web/src 下所有 .ts / .tsx 文件路径（相对 WEB_SRC）。
 */
function listTsFiles(dir: string): string[] {
  const result: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === "dist") continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      result.push(...listTsFiles(full));
    } else if (
      entry.isFile() &&
      (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx"))
    ) {
      result.push(full);
    }
  }
  return result;
}

/** 把绝对路径转成相对 WEB_SRC 的 posix 路径，方便断言 */
function rel(p: string): string {
  return path.relative(WEB_SRC, p).split(path.sep).join("/");
}

const TOOLS_IMPORT_RE =
  /from\s+['"]@\/network\/tools(?:\/[\w-]+)?['"]/;

describe("network import boundary", () => {
  it("only_network_package_files_import_tools", () => {
    const all = listTsFiles(WEB_SRC);
    const violations: string[] = [];
    for (const file of all) {
      const r = rel(file);
      // 允许 network 包内文件 import
      if (r.startsWith("network/")) continue;
      const content = fs.readFileSync(file, "utf8");
      if (TOOLS_IMPORT_RE.test(content)) {
        violations.push(r);
      }
    }
    expect(violations).toEqual([]);
  });

  it("network_package_files_can_import_tools_internally", () => {
    // sanity check：本测试运行依赖 listTsFiles 实现正确——
    // 既然 manager.ts 内确实 import 了 tools，这条断言应通过。
    const managerPath = path.join(WEB_SRC, "network", "manager.ts");
    const content = fs.readFileSync(managerPath, "utf8");
    expect(content).toMatch(/from\s+['"]\.\/tools['"]/);
  });
});
