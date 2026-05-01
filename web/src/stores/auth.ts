import { create } from "zustand";
import { apiGet, apiPost, ApiError } from "@/lib/api";
import type { LoginRequest } from "@/protocol";

/**
 * 认证状态：
 * - `_checked` 表示是否已经发起过 checkAuth；防止 AuthGuard 在 mount 时就 redirect
 * - `authenticated`：当前 cookie session 是否有效
 *
 * 后端约定：
 * - GET  /api/auth/me      → 200 { ok: true } | 401
 * - POST /api/auth/login   → 200 + Set-Cookie | 401 | 429
 * - POST /api/auth/logout  → 204
 */
interface AuthState {
  authenticated: boolean;
  _checked: boolean;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** 软登出（401 拦截器调用，无需再次打 logout 端点） */
  logoutLocal: () => void;
  checkAuth: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set) => ({
  authenticated: false,
  _checked: false,

  login: async (password: string) => {
    const body: LoginRequest = { password };
    await apiPost<void>("/api/auth/login", body);
    set({ authenticated: true, _checked: true });
  },

  logout: async () => {
    try {
      await apiPost<void>("/api/auth/logout");
    } catch (err) {
      // 401 / 网络错误都接受为已登出
      if (!(err instanceof ApiError)) throw err;
    }
    set({ authenticated: false, _checked: true });
    if (typeof window !== "undefined" && window.location.pathname !== "/login") {
      window.location.href = "/login";
    }
  },

  logoutLocal: () => {
    set({ authenticated: false, _checked: true });
  },

  checkAuth: async () => {
    try {
      await apiGet<void>("/api/auth/me");
      set({ authenticated: true, _checked: true });
    } catch {
      set({ authenticated: false, _checked: true });
    }
  },
}));
