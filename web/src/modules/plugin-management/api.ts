import { apiGet, apiPatch } from "@/lib/api";
import type {
  PluginToolDTO,
  PluginToolsResponseDTO,
  UpdatePluginToolRequest,
} from "@/protocol";

export function listPluginTools(): Promise<PluginToolsResponseDTO> {
  return apiGet<PluginToolsResponseDTO>("/api/manage/plugins");
}

export function updatePluginTool(
  toolId: string,
  body: UpdatePluginToolRequest,
): Promise<PluginToolDTO> {
  return apiPatch<PluginToolDTO>(
    `/api/manage/plugins/${encodeURIComponent(toolId)}`,
    body,
  );
}
