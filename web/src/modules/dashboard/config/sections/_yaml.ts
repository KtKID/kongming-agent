/**
 * dashboard/config/sections 内部小工具：从整段 setting.yaml 原文里
 * 切出某个顶层 key（如 model / runtime）的子树，作为 YamlPreview 输入。
 *
 * 与 `components/SafetyRulesView.tsx::extractSafetySection` 同源同思路：
 * 1. 按行扫描，找到行首 `^<key>:` 的起点
 * 2. 从下一行起继续扫，遇到下一个顶层 key（行首非空白 + 字母/_/数字 + `:`）停下
 * 3. 切片拼回字符串
 *
 * 这里没有复用 SafetyRulesView 内的实现，因为它硬编码了 "safety"，把它通用化会改动
 * sibling 文件超出本 task 范围。本文件只服务 sections/ 下的 ModelSection / RuntimeSection
 * 等通用 section，section 的 YamlPreview 都走这里。
 */

/**
 * 切出 `<topKey>:` 段子串。
 *
 * 边角：
 * - content 为空 / 无该段 → 返回提示文案 `（无 <topKey> 段）`
 * - <topKey>: 是最后一段 → 切到文件末尾
 */
export function extractTopLevelSection(content: string, topKey: string): string {
  const placeholder = `（无 ${topKey} 段）`;
  if (!content) return placeholder;

  const lines = content.split("\n");
  const startPattern = new RegExp(`^${escapeRegex(topKey)}\\s*:`);
  let sIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    if (startPattern.test(lines[i])) {
      sIdx = i;
      break;
    }
  }
  if (sIdx < 0) return placeholder;

  // 下一个"顶层 key"：行首非空白 + 字母/下划线/数字开头 + 任意符号 + `:`
  let eIdx = lines.length;
  for (let i = sIdx + 1; i < lines.length; i++) {
    if (/^[A-Za-z_][\w.-]*\s*:/.test(lines[i])) {
      eIdx = i;
      break;
    }
  }
  return lines.slice(sIdx, eIdx).join("\n");
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
