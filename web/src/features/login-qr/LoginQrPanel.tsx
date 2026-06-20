import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Check, Copy, Loader2, RefreshCw } from "lucide-react";
import QRCode from "qrcode";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, RateLimitedError } from "@/lib/api";
import {
  confirmLoginQrSession,
  createLoginQrSession,
  getLoginQrStatus,
  type CreateLoginQrSessionResponse,
  type LoginQrClaimView,
  type LoginQrSessionStatus,
  type LoginQrStatusResponse,
} from "./LoginQrManager";

type PanelPhase = "idle" | "loading" | "ready" | "confirming" | "error";

const ACTIVE_STATUSES: LoginQrSessionStatus[] = [
  "pending_scan",
  "pending_confirm",
  "confirmed",
];

function formatError(error: unknown): string {
  if (error instanceof RateLimitedError) {
    return `请求过于频繁，请 ${error.retryAfterSeconds}s 后重试`;
  }
  if (error instanceof ApiError) {
    switch (error.errorCode) {
      case "server_origin_required":
        return "需要配置 web.server_origin";
      case "server_origin_invalid_scheme":
        return "server_origin 需要使用 HTTPS 域名或 HTTP 私网 IP";
      case "server_origin_loopback":
        return "二维码地址需要使用公网域名或局域网私网 IP";
      case "server_origin_not_lan_ip":
        return "HTTP 模式只支持局域网私网 IP";
      case "server_origin_public_host_invalid":
        return "HTTPS 模式需要使用公网域名";
      case "browser_token_mismatch":
        return "二维码状态已失效，请刷新";
      case "login_qr_not_found":
        return "二维码不存在，请刷新";
      case "invalid_credentials":
        return "密码错误";
      case "login_qr_expired":
        return "二维码已过期";
      case "login_qr_already_claimed":
        return "二维码已被使用，请刷新";
      case "approval_denied":
        return "登录已取消";
      case "login_qr_already_exchanged":
        return "XSpace 已完成登录";
      default:
        return error.detail;
    }
  }
  return error instanceof Error ? error.message : String(error);
}

function statusText(status: LoginQrSessionStatus, claim: LoginQrClaimView | null): string {
  if (status === "pending_scan") return "等待 XSpace 扫码";
  if (status === "pending_confirm" && claim) return `等待确认 ${claim.label}`;
  if (status === "confirmed") return "已确认，等待 XSpace 完成登录";
  if (status === "exchanged") return "XSpace 已登录";
  if (status === "expired") return "二维码已过期";
  if (status === "cancelled") return "二维码已取消";
  return "等待扫码";
}

function secondsLeft(expiresAt: string | null, nowMs: number): number {
  if (!expiresAt) return 0;
  return Math.max(0, Math.ceil((Date.parse(expiresAt) - nowMs) / 1000));
}

interface LoginQrPanelProps {
  pollIntervalMs?: number;
}

export function LoginQrPanel({ pollIntervalMs = 1000 }: LoginQrPanelProps) {
  const [phase, setPhase] = useState<PanelPhase>("idle");
  const [session, setSession] = useState<CreateLoginQrSessionResponse | null>(null);
  const [status, setStatus] = useState<LoginQrStatusResponse | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const ttlSeconds = secondsLeft(session?.expires_at ?? null, nowMs);
  const claim = status?.claim ?? null;
  const currentStatus = status?.status ?? session?.status ?? "pending_scan";
  const canConfirm =
    currentStatus === "pending_confirm" &&
    claim?.status === "pending_confirm" &&
    password.length > 0 &&
    phase !== "confirming";

  const createSession = useCallback(async () => {
    setPhase("loading");
    setError("");
    setPassword("");
    setCopied(false);
    try {
      const created = await createLoginQrSession();
      setSession(created);
      setStatus({
        login_qr_id: created.login_qr_id,
        status: created.status,
        expires_at: created.expires_at,
        claim: null,
      });
      const dataUrl = await QRCode.toDataURL(created.qr_payload, {
        width: 192,
        margin: 1,
        errorCorrectionLevel: "M",
        color: { dark: "#111827", light: "#ffffff" },
      });
      setQrDataUrl(dataUrl);
      setPhase("ready");
    } catch (err) {
      setSession(null);
      setStatus(null);
      setQrDataUrl("");
      setError(formatError(err));
      setPhase("error");
    }
  }, []);

  useEffect(() => {
    void createSession();
  }, [createSession]);

  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!session || !ACTIVE_STATUSES.includes(currentStatus)) return;
    const id = window.setInterval(async () => {
      try {
        const next = await getLoginQrStatus(session.login_qr_id, session.browser_token);
        setStatus(next);
        setError("");
      } catch (err) {
        setError(formatError(err));
      }
    }, pollIntervalMs);
    return () => window.clearInterval(id);
  }, [currentStatus, pollIntervalMs, session]);

  useEffect(() => {
    if (ttlSeconds > 0 || !session) return;
    setStatus((prev) =>
      prev ? { ...prev, status: "expired" } : prev,
    );
  }, [session, ttlSeconds]);

  const onCopy = async () => {
    if (!session?.copy_url || !navigator.clipboard) return;
    await navigator.clipboard.writeText(session.copy_url);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  const onConfirm = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !claim || !canConfirm) return;
    setPhase("confirming");
    setError("");
    try {
      const result = await confirmLoginQrSession(
        session.login_qr_id,
        session.browser_token,
        claim.claim_id,
        password,
      );
      setStatus((prev) =>
        prev ? { ...prev, status: result.status, claim: prev.claim } : prev,
      );
      setPassword("");
      setPhase("ready");
    } catch (err) {
      setError(formatError(err));
      setPhase("ready");
    }
  };

  const originLabel = useMemo(() => {
    if (!session) return "";
    return session.server_origin.mode === "lan_ip" ? "局域网" : "公网";
  }, [session]);

  return (
    <section className="flex min-h-[360px] flex-col gap-4 md:border-l md:border-border md:pl-8">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">XSpace 扫码登录</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {session ? `${originLabel} · ${statusText(currentStatus, claim)}` : "正在生成二维码"}
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void createSession()}
          disabled={phase === "loading"}
          aria-label="刷新二维码"
          title="刷新二维码"
        >
          <RefreshCw className={phase === "loading" ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
        </Button>
      </div>

      <div className="flex flex-col items-center gap-3 rounded-lg border border-border bg-background p-4">
        <div className="grid h-48 w-48 place-items-center rounded-md bg-white p-2">
          {qrDataUrl ? (
            <img src={qrDataUrl} alt="XSpace 扫码登录二维码" className="h-full w-full" />
          ) : (
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          )}
        </div>
        <div className="flex w-full items-center justify-between gap-2 text-xs text-muted-foreground">
          <span>{ttlSeconds > 0 ? `${ttlSeconds}s` : "已过期"}</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void onCopy()}
            disabled={!session?.copy_url}
            aria-label="复制登录链接"
            title="复制登录链接"
          >
            {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
          </Button>
        </div>
      </div>

      {claim ? (
        <div className="rounded-md border border-border px-3 py-2 text-xs">
          <div className="font-medium">{claim.label}</div>
          <div className="mt-1 text-muted-foreground">
            {claim.platform} · {claim.app_version} · {claim.device_id}
          </div>
        </div>
      ) : null}

      <form onSubmit={onConfirm} className="flex flex-col gap-2">
        <Input
          type="password"
          placeholder="确认密码"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          disabled={currentStatus !== "pending_confirm" || phase === "confirming"}
          aria-label="扫码登录确认密码"
        />
        <Button type="submit" disabled={!canConfirm}>
          {phase === "confirming" ? "确认中..." : "确认授权"}
        </Button>
      </form>

      {error ? (
        <div role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      ) : null}
    </section>
  );
}
