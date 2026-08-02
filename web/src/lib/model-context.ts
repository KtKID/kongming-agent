/**
 * 模型上下文窗口（context window）上限内置表。
 *
 * ⚠️ **占位值**：以下数字基于 2026-05 公开文档/估计，**合并前必须由用户核实**。
 * 查询参考：
 * - OpenAI: `GET https://api.openai.com/v1/models/{model_id}` 返回 `context_window`
 * - Anthropic: 官方文档 https://docs.anthropic.com/en/docs/about-claude/models
 * - MiniMax / 智谱 / 阿里 / DeepSeek：各家官方文档
 *
 * 命中规则（按 key 长度倒序，prefix match）：
 *   - exact match 优先
 *   - 否则按 key 长度倒序尝试 prefix match（避免短前缀误命中）
 *   - 未命中返回 `null`，UI 退化为不显示百分比
 */
export const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  // === OpenAI ===
  "gpt-4o": 128_000,
  "gpt-4-turbo": 128_000,
  "gpt-4": 8_192,
  "gpt-3.5-turbo": 16_385,
  // === Anthropic ===
  "claude-opus-4": 1_000_000, // 用户 2026-05-14 确认
  "claude-sonnet-4": 200_000,
  "claude-haiku-4": 200_000,
  "claude-3-5-sonnet": 200_000,
  "claude-3-opus": 200_000,
  // === 国产 ===
  // GLM-5.2 官方支持 1M context；远端 model 名是 glm-5.2（catalog preset 的 model
  // 字段不带 [1m] 后缀，[1m] 仅在 display_name 作本地 UI 标识）。必须与后端
  // `src/hosts/web/usage/usage_token_v2/_model_context_table.py` 字面对齐。
  // 来源：https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2
  "glm-5.2": 1_000_000,
  "glm-5.1": 128_000,
  "glm-4": 128_000,
  "MiniMax-M3": 200_000,
  "deepseek-chat": 64_000,
  // === 本地 / 小模型 ===
  "gemma-4-e4b-it": 8_192,
};

/**
 * 查模型 context window 上限。
 *
 * @param modelName 模型名（精确或前缀；空字符串返回 null）
 * @returns 上限 token 数，未命中返回 null
 */
export function lookupContextWindow(modelName: string): number | null {
  if (!modelName) return null;
  // exact match
  if (modelName in MODEL_CONTEXT_WINDOWS) {
    return MODEL_CONTEXT_WINDOWS[modelName];
  }
  // prefix match（按 key 长度倒序）
  const keys = Object.keys(MODEL_CONTEXT_WINDOWS).sort(
    (a, b) => b.length - a.length,
  );
  for (const key of keys) {
    if (modelName.startsWith(key)) {
      return MODEL_CONTEXT_WINDOWS[key];
    }
  }
  return null;
}
