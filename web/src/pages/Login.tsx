import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAuthStore } from "@/stores/auth";
import { apiPost, ApiError, RateLimitedError } from "@/lib/api";

/**
 * /login 页：单密码登录 + 忘记密码重置流程。
 */
export function LoginPage() {
  const navigate = useNavigate();
  const login = useAuthStore((s) => s.login);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [retryCountdown, setRetryCountdown] = useState(0);

  const [resetOpen, setResetOpen] = useState(false);
  const [resetNewPw, setResetNewPw] = useState("");
  const [resetError, setResetError] = useState("");
  const [resetPending, setResetPending] = useState(false);
  const [resetDone, setResetDone] = useState(false);

  useEffect(() => {
    if (retryCountdown <= 0) return;
    const id = window.setTimeout(() => setRetryCountdown((c) => c - 1), 1000);
    return () => window.clearTimeout(id);
  }, [retryCountdown]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (pending || retryCountdown > 0) return;
    setError("");
    setPending(true);
    try {
      await login(password);
      navigate("/chat", { replace: true });
    } catch (err) {
      if (err instanceof RateLimitedError) {
        setError(`请求过于频繁，请 ${err.retryAfterSeconds}s 后重试`);
        setRetryCountdown(err.retryAfterSeconds);
      } else if (err instanceof ApiError && err.status === 401) {
        setError("密码错误");
      } else {
        const msg = err instanceof Error ? err.message : String(err);
        setError(`登录失败：${msg}`);
      }
    } finally {
      setPending(false);
    }
  };

  const onReset = async (e: FormEvent) => {
    e.preventDefault();
    if (resetPending) return;
    setResetError("");
    setResetPending(true);
    try {
      await apiPost("/api/auth/reset-password", {
        new_password: resetNewPw,
      });
      setResetDone(true);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setResetError(`重置失败：${msg}`);
    } finally {
      setResetPending(false);
    }
  };

  const openReset = () => {
    setResetOpen(true);
    setResetNewPw("");
    setResetError("");
    setResetDone(false);
  };

  const disabled = pending || retryCountdown > 0;

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-background p-4">
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.24, ease: [0, 0, 0.2, 1] }}
        className="w-full max-w-sm rounded-xl border border-border bg-card p-8 shadow-lg"
      >
        <div className="mb-6 flex flex-col items-center gap-2">
          <div className="h-10 w-10 rounded-lg bg-accent" />
          <h1 className="text-xl font-semibold tracking-tight">
            kongming-agent
          </h1>
          <p className="text-xs text-muted-foreground">输入密码继续</p>
        </div>
        <form onSubmit={onSubmit} className="flex flex-col gap-3">
          <Input
            type="password"
            placeholder="密码"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={disabled}
            aria-label="密码"
          />
          {error ? (
            <div
              role="alert"
              className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive"
            >
              {error}
              {retryCountdown > 0 ? `（${retryCountdown}s）` : null}
            </div>
          ) : null}
          <Button type="submit" disabled={disabled || !password}>
            {pending ? "登录中..." : "登录"}
          </Button>
          <button
            type="button"
            onClick={openReset}
            className="text-xs text-muted-foreground hover:text-foreground hover:underline"
          >
            忘记密码？
          </button>
        </form>
      </motion.div>

      <Dialog open={resetOpen} onOpenChange={setResetOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重置密码</DialogTitle>
            <DialogDescription>
              直接输入新密码即可重置，无需额外验证。
            </DialogDescription>
          </DialogHeader>
          {resetDone ? (
            <div className="py-4 text-center text-sm text-green-600">
              密码已重置，请用新密码登录。
            </div>
          ) : (
            <form onSubmit={onReset} className="flex flex-col gap-3">
              <Input
                type="password"
                placeholder="新密码"
                value={resetNewPw}
                onChange={(e) => setResetNewPw(e.target.value)}
                disabled={resetPending}
                aria-label="新密码"
                autoFocus
              />
              {resetError ? (
                <div className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
                  {resetError}
                </div>
              ) : null}
              <DialogFooter>
                <Button type="submit" disabled={resetPending || !resetNewPw}>
                  {resetPending ? "重置中..." : "确认重置"}
                </Button>
              </DialogFooter>
            </form>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
