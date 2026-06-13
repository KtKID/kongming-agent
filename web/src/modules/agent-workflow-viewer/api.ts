import { apiGet } from "@/lib/api";
import type {
  ConversationDTO,
  WorkflowArtifactContentDTO,
  WorkflowDetailDTO,
  WorkflowListDTO,
} from "./types";

const workflowPath = (threadId: string) =>
  `/api/threads/${encodeURIComponent(threadId)}/agent-workflows`;

export function fetchAgentWorkflows(threadId: string): Promise<WorkflowListDTO> {
  return apiGet<WorkflowListDTO>(workflowPath(threadId));
}

export function fetchAgentWorkflowDetail(
  threadId: string,
  workflowId: string,
): Promise<WorkflowDetailDTO> {
  return apiGet<WorkflowDetailDTO>(
    `${workflowPath(threadId)}/${encodeURIComponent(workflowId)}`,
  );
}

export function fetchAgentWorkflowConversation(params: {
  threadId: string;
  workflowId: string;
  taskRunId: string;
  cursor?: number;
  limit?: number;
}): Promise<ConversationDTO> {
  const search = new URLSearchParams();
  if (params.cursor != null) search.set("cursor", String(params.cursor));
  if (params.limit != null) search.set("limit", String(params.limit));
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return apiGet<ConversationDTO>(
    `${workflowPath(params.threadId)}/${encodeURIComponent(params.workflowId)}` +
      `/subagents/${encodeURIComponent(params.taskRunId)}/conversation${suffix}`,
  );
}

export function fetchAgentWorkflowArtifact(params: {
  threadId: string;
  workflowId: string;
  artifactId: string;
}): Promise<WorkflowArtifactContentDTO> {
  return apiGet<WorkflowArtifactContentDTO>(
    `${workflowPath(params.threadId)}/${encodeURIComponent(params.workflowId)}` +
      `/artifacts/${encodeURIComponent(params.artifactId)}`,
  );
}

export function fetchThreadUsage(threadId: string): Promise<unknown> {
  return apiGet<unknown>(`/api/threads/${encodeURIComponent(threadId)}/usage`);
}
