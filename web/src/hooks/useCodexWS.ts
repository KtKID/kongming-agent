import { useEffect, useRef, useState } from "react";
import { CodexSocket, type SocketState } from "@/lib/codex-ws";

/**
 * 给定 codex thread 的 thread_id，建一个 `/ws/codex` 长连接。
 *
 * 和 `useClaudeCodeWS`（claude-code 路径）状态机 / StrictMode 安全策略一致；
 * 上层在 Chat.tsx 按 `thread.backend_kind` 选用对应 hook，不要在同一组件里
 * 同时调两个 hook —— React rules of hooks。
 */
export function useCodexWS(threadId: string | undefined) {
  const [socket, setSocket] = useState<CodexSocket | null>(null);
  const [state, setState] = useState<SocketState>("closed");
  const ref = useRef<CodexSocket | null>(null);

  useEffect(() => {
    if (!threadId) {
      setSocket(null);
      setState("closed");
      return;
    }
    const s = new CodexSocket(threadId);
    ref.current = s;
    setSocket(s);
    const offState = s.onState((next) => setState(next));
    s.connect();
    return () => {
      offState();
      s.close();
      if (ref.current === s) ref.current = null;
    };
  }, [threadId]);

  return { socket, state };
}
