import { apiGet, apiPost } from "@/lib/api";

import type {
  ConnectProviderRequest,
  ConnectedModelFamily,
  ProviderActionResponse,
  ProviderCatalogItem,
  ProviderConnection,
  TestProviderRequest,
} from "./types";

export function getProviderCatalog(): Promise<ProviderCatalogItem[]> {
  return apiGet<ProviderCatalogItem[]>("/api/model-providers/catalog");
}

export function getProviderConnections(): Promise<ProviderConnection[]> {
  return apiGet<ProviderConnection[]>("/api/model-providers/connections");
}

export function getConnectedModelFamilies(): Promise<ConnectedModelFamily[]> {
  return apiGet<ConnectedModelFamily[]>("/api/model-providers/model-families");
}

export function testProvider(
  providerId: string,
  body: TestProviderRequest,
): Promise<ProviderActionResponse> {
  return apiPost<ProviderActionResponse>(
    `/api/model-providers/${encodeURIComponent(providerId)}/test`,
    body,
  );
}

export function connectProvider(
  providerId: string,
  body: ConnectProviderRequest,
): Promise<ProviderActionResponse> {
  return apiPost<ProviderActionResponse>(
    `/api/model-providers/${encodeURIComponent(providerId)}/connect`,
    body,
  );
}

export function testCurrentProvider(
  providerId: string,
): Promise<ProviderActionResponse> {
  return apiPost<ProviderActionResponse>(
    `/api/model-providers/${encodeURIComponent(providerId)}/test-current`,
  );
}
