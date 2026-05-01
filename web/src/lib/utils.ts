import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * shadcn/ui 标准工具：拼接 + 合并 Tailwind class，避免重复声明冲突。
 * 全前端只用这一个函数；不允许直接拼字符串绕过。
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
