import { create } from "zustand";

type Theme = "light" | "dark" | "system";
const STORAGE_KEY = "kongming-theme";

function getSystemTheme(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function applyTheme(theme: Theme) {
  const resolved = theme === "system" ? getSystemTheme() : theme;
  document.documentElement.classList.toggle("dark", resolved === "dark");
  document.documentElement.dataset.kongmingTheme =
    resolved === "dark" ? "obsidian-dark" : "guofeng-light";
}

interface ThemeState {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

export const useThemeStore = create<ThemeState>((set) => {
  // 初始化：读 localStorage，默认 system
  const stored = localStorage.getItem(STORAGE_KEY) as Theme | null;
  const initial: Theme = stored && ["light", "dark", "system"].includes(stored) ? stored : "system";
  applyTheme(initial);

  // 监听系统主题变化（system 模式下自动跟随）
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const current = (localStorage.getItem(STORAGE_KEY) as Theme) ?? "system";
    if (current === "system") applyTheme("system");
  });

  return {
    theme: initial,
    setTheme: (theme: Theme) => {
      localStorage.setItem(STORAGE_KEY, theme);
      applyTheme(theme);
      set({ theme });
    },
  };
});
