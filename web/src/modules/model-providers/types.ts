export type ProviderConnectionStatus = "connected" | "disconnected" | "error";

export interface ProviderCatalogItem {
  providerId: string;
  displayName: string;
  regionLabel: string;
  description: string;
  logoText: string;
}

export interface ProviderConnection {
  providerId: string;
  status: ProviderConnectionStatus;
  model: string | null;
  authLabel: string | null;
}

export interface ProviderListItem extends ProviderCatalogItem {
  status: ProviderConnectionStatus;
  model: string | null;
  authLabel: string | null;
}

export interface ConnectedModelFamily {
  providerId: string;
  providerLabel: string;
  familyId: string;
  displayName: string;
  presetId: string;
  model: string;
  connected: boolean;
}

export interface TestProviderRequest {
  apiKey?: string;
}

export interface ConnectProviderRequest {
  apiKey: string;
}

export interface ProviderActionResponse {
  providerId: string;
  ok: boolean;
  message: string;
  connection?: ProviderConnection;
}
