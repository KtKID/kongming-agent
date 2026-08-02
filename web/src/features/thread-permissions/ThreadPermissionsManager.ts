/**
 * thread permissions 功能的唯一前端门户。
 *
 * 负责按 thread 加载、编辑并使用 revision 做 CAS 保存；具体视图固定在 internal，
 * 外部模块只通过本 Manager 和公开类型接入。
 */

import {
  createElement,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactElement,
} from "react";

import { ApiError, apiGet, apiPut } from "@/lib/api";
import type {
  PermissionRuleDTO,
  ThreadPermissionsDTO,
  UpdateThreadPermissionsRequest,
} from "@/protocol";

import { ThreadPermissionsView } from "./internal/ThreadPermissionsView";

export type ThreadPermissionsStatus =
  | "loading"
  | "ready"
  | "saving"
  | "saved"
  | "conflict"
  | "error";

export interface ThreadPermissionsManagerProps {
  threadId: string;
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.detail || error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

function normalizeRules(rules: PermissionRuleDTO[]): PermissionRuleDTO[] {
  const seen = new Set<string>();
  const normalized: PermissionRuleDTO[] = [];
  for (const rule of rules) {
    const expression = rule.expression.trim();
    const scope_cwd = rule.scope_cwd?.trim() || null;
    if (!expression) continue;
    const identity = JSON.stringify([expression, scope_cwd]);
    if (seen.has(identity)) continue;
    seen.add(identity);
    normalized.push({ expression, scope_cwd });
  }
  return normalized;
}

export function ThreadPermissionsManager({
  threadId,
}: ThreadPermissionsManagerProps): ReactElement {
  const [status, setStatus] = useState<ThreadPermissionsStatus>("loading");
  const [snapshot, setSnapshot] = useState<ThreadPermissionsDTO | null>(null);
  const [allowDraft, setAllowDraft] = useState<PermissionRuleDTO[]>([]);
  const [denyDraft, setDenyDraft] = useState<PermissionRuleDTO[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const requestSequence = useRef(0);

  const load = useCallback(async (): Promise<void> => {
    const sequence = requestSequence.current + 1;
    requestSequence.current = sequence;
    setStatus("loading");
    setSnapshot(null);
    setAllowDraft([]);
    setDenyDraft([]);
    setErrorMessage(null);
    setLoadFailed(false);

    try {
      const next = await apiGet<ThreadPermissionsDTO>(
        `/api/threads/${encodeURIComponent(threadId)}/permissions`,
      );
      if (requestSequence.current !== sequence) return;
      setSnapshot(next);
      setAllowDraft(next.allow);
      setDenyDraft(next.deny);
      setStatus("ready");
    } catch (error) {
      if (requestSequence.current !== sequence) return;
      setStatus("error");
      setLoadFailed(true);
      setErrorMessage(describeError(error));
    }
  }, [threadId]);

  useEffect(() => {
    void load();
    return () => {
      requestSequence.current += 1;
    };
  }, [load]);

  const save = useCallback(async (): Promise<void> => {
    if (snapshot === null || status === "saving") return;

    const body: UpdateThreadPermissionsRequest = {
      thread_id: snapshot.thread_id,
      revision: snapshot.revision,
      allow: normalizeRules(allowDraft),
      deny: normalizeRules(denyDraft),
    };
    const sequence = requestSequence.current;
    setStatus("saving");
    setErrorMessage(null);
    setLoadFailed(false);

    try {
      const next = await apiPut<ThreadPermissionsDTO>(
        `/api/threads/${encodeURIComponent(threadId)}/permissions`,
        body,
      );
      if (requestSequence.current !== sequence) return;
      setSnapshot(next);
      setAllowDraft(next.allow);
      setDenyDraft(next.deny);
      setStatus("saved");
    } catch (error) {
      if (requestSequence.current !== sequence) return;
      if (error instanceof ApiError && error.status === 409) {
        setStatus("conflict");
      } else {
        setStatus("error");
      }
      setErrorMessage(describeError(error));
    }
  }, [allowDraft, denyDraft, snapshot, status, threadId]);

  return createElement(ThreadPermissionsView, {
    threadId,
    snapshot,
    status,
    allowDraft,
    denyDraft,
    errorMessage,
    loadFailed,
    onAllowDraftChange: setAllowDraft,
    onDenyDraftChange: setDenyDraft,
    onSave: save,
    onReload: load,
  });
}
