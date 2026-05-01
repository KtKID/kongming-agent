import { useEffect } from "react";
import { Navigate, Outlet } from "react-router-dom";
import { useAuthStore } from "@/stores/auth";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * 路由守卫：
 * - 未发起 checkAuth → 触发并显示 skeleton（只触发一次，由 _checked 锁）
 * - 已检查未认证 → redirect /login
 * - 已认证 → <Outlet />
 *
 * StrictMode 双 mount 安全：依赖 _checked 标记，幂等。
 */
export function AuthGuard() {
  const authenticated = useAuthStore((s) => s.authenticated);
  const checked = useAuthStore((s) => s._checked);
  const checkAuth = useAuthStore((s) => s.checkAuth);

  useEffect(() => {
    if (!checked) {
      void checkAuth();
    }
  }, [checked, checkAuth]);

  if (!checked) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <Skeleton className="h-10 w-10 rounded-lg" />
          <Skeleton className="h-3 w-24" />
        </div>
      </div>
    );
  }
  if (!authenticated) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}
