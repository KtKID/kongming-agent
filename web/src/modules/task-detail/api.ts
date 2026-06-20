import { apiGet } from "@/lib/api";
import type {
  ThreadArtifactContentDTO,
  ThreadArtifactListDTO,
} from "./types";

const artifactPath = (threadId: string) =>
  `/api/threads/${encodeURIComponent(threadId)}/artifacts`;

export function fetchThreadArtifacts(threadId: string): Promise<ThreadArtifactListDTO> {
  return apiGet<ThreadArtifactListDTO>(artifactPath(threadId));
}

export function fetchThreadArtifactContent(params: {
  threadId: string;
  artifactId: string;
}): Promise<ThreadArtifactContentDTO> {
  return apiGet<ThreadArtifactContentDTO>(
    `${artifactPath(params.threadId)}/${encodeURIComponent(params.artifactId)}`,
  );
}
