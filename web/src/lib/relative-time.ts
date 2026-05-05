/**
 * 把 Unix 时间戳（秒）转成相对时间字符串。
 *
 * - <60s → "刚刚"
 * - <60min → "X 分钟前"
 * - <24h → "X 小时前"
 * - else → "YYYY-MM-DD"
 *
 * @param unixSeconds  目标时间（Unix 秒）
 * @param now          可选的"当前时间"（Unix 秒）；测试用，不传走 Date.now()
 */
export function formatRelative(unixSeconds: number, now?: number): string {
  const nowSec = now ?? Date.now() / 1000;
  const diff = nowSec - unixSeconds;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  const d = new Date(unixSeconds * 1000);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
