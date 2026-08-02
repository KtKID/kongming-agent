import { apiGet } from "@/lib/api";
import type { ConversationReferenceTemplate } from "@/protocol";

export type SlashCatalogItemKind =
  | "workflow_strategy"
  | "workflow_run"
  | "command"
  | "skill";

export type SlashCatalogActionKind =
  | "insert_text"
  | "bind_reference"
  | "guide_payload"
  | "open_viewer";

export interface SlashCatalogDiagnostic {
  code: string;
  severity: "info" | "warning" | "error";
  message: string;
  path?: string | null;
}

export interface SlashCatalogGroup {
  id: string;
  title: string;
  description: string;
  order: number;
  item_count: number;
  diagnostics: SlashCatalogDiagnostic[];
}

export interface SlashCatalogItem {
  id: string;
  group_id: string;
  kind: SlashCatalogItemKind;
  title: string;
  description: string;
  source_ref: string;
  order: number;
  section_id?: string | null;
  slash?: string | null;
  insert_text?: string | null;
  action: SlashCatalogActionKind;
  reference_template?: ConversationReferenceTemplate | null;
  enabled: boolean;
  metadata: Record<string, unknown>;
  diagnostics: SlashCatalogDiagnostic[];
}

export interface SlashCatalogGroupsResponse {
  groups: SlashCatalogGroup[];
}

export interface SlashCatalogGroupItemsResponse {
  group: SlashCatalogGroup;
  items: SlashCatalogItem[];
}

export async function fetchSlashCatalogGroups(
  threadId?: string,
): Promise<SlashCatalogGroup[]> {
  const response = await apiGet<SlashCatalogGroupsResponse>(
    withThreadId("/api/slash-catalog", threadId),
  );
  return response.groups;
}

export async function fetchSlashCatalogGroupItems(
  groupId: string,
  threadId?: string,
): Promise<SlashCatalogGroupItemsResponse> {
  return apiGet<SlashCatalogGroupItemsResponse>(
    withThreadId(
      `/api/slash-catalog/groups/${encodeURIComponent(groupId)}`,
      threadId,
    ),
  );
}

export async function fetchSlashCatalogItems(
  threadId?: string,
): Promise<SlashCatalogItem[]> {
  const groups = await fetchSlashCatalogGroups(threadId);
  const responses = await Promise.all(
    groups.map((group) => fetchSlashCatalogGroupItems(group.id, threadId)),
  );
  return responses.flatMap((response) => response.items);
}

function withThreadId(path: string, threadId?: string): string {
  if (!threadId) return path;
  const params = new URLSearchParams({ thread_id: threadId });
  return `${path}?${params.toString()}`;
}
