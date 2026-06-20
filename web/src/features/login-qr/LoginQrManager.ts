import { apiGetWithHeaders, apiPost } from "@/lib/api";

export interface LoginQrOriginView {
  mode: "public_https" | "lan_ip";
  origin: string;
  scheme: "https" | "http";
  host: string;
  port: number | null;
}

export interface CreateLoginQrSessionResponse {
  login_qr_id: string;
  browser_token: string;
  status: "pending_scan";
  expires_at: string;
  server_origin: LoginQrOriginView;
  server: string;
  qr_payload: string;
  copy_url: string;
}

export type LoginQrSessionStatus =
  | "pending_scan"
  | "pending_confirm"
  | "confirmed"
  | "exchanged"
  | "expired"
  | "cancelled";

export type LoginQrClaimStatus =
  | "pending_confirm"
  | "approved"
  | "denied"
  | "exchanged";

export interface LoginQrClaimView {
  claim_id: string;
  device_id: string;
  label: string;
  platform: "android";
  app_version: string;
  capabilities: Record<string, boolean>;
  status: LoginQrClaimStatus;
  created_at: string;
}

export interface LoginQrStatusResponse {
  login_qr_id: string;
  status: LoginQrSessionStatus;
  expires_at: string;
  claim: LoginQrClaimView | null;
}

export interface ConfirmLoginQrResponse {
  status: "confirmed";
  poll_after_ms: number;
}

const LOGIN_QR_TOKEN_HEADER = "X-Kongming-Login-Qr-Token";

export async function createLoginQrSession(): Promise<CreateLoginQrSessionResponse> {
  return apiPost<CreateLoginQrSessionResponse>(
    "/api/xspace/mobile/login-qr-sessions",
    {
      protocol_version: "1",
      client: "kongming-login",
      requested_scopes: ["webview", "thread.read", "approval.resolve"],
    },
  );
}

export async function getLoginQrStatus(
  loginQrId: string,
  browserToken: string,
): Promise<LoginQrStatusResponse> {
  return apiGetWithHeaders<LoginQrStatusResponse>(
    `/api/xspace/mobile/login-qr-sessions/${encodeURIComponent(loginQrId)}`,
    { [LOGIN_QR_TOKEN_HEADER]: browserToken },
  );
}

export async function confirmLoginQrSession(
  loginQrId: string,
  browserToken: string,
  claimId: string,
  password: string,
): Promise<ConfirmLoginQrResponse> {
  return apiPost<ConfirmLoginQrResponse>(
    `/api/xspace/mobile/login-qr-sessions/${encodeURIComponent(loginQrId)}/confirm`,
    {
      browser_token: browserToken,
      claim_id: claimId,
      password,
    },
  );
}
