import type {
  ConnectedModelFamilyDTO,
  ConnectProviderRequestDTO,
  ProviderActionResponseDTO,
  ProviderCatalogItemDTO,
  ProviderConnectionDTO,
  ProviderConnectionStatus,
  TestProviderRequestDTO,
} from "@/protocol";

export type { ProviderConnectionStatus };
export type ProviderCatalogItem = ProviderCatalogItemDTO;
export type ProviderConnection = ProviderConnectionDTO;

export interface ProviderListItem extends ProviderCatalogItem {
  status: ProviderConnectionStatus;
  model: string | null;
  authLabel: string | null;
}

export type ConnectedModelFamily = ConnectedModelFamilyDTO;

export type TestProviderRequest = TestProviderRequestDTO;
export type ConnectProviderRequest = ConnectProviderRequestDTO;

export type ProviderActionResponse = ProviderActionResponseDTO;
