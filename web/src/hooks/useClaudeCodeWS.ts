import { useEffect, useRef, useState } from "react";
import { ClaudeCodeSocket, type SocketState } from "@/lib/claude-ws";

/**
 * 给定 claude_code thread 的 thread_id，建一个 `/ws/claude-code` 长连接。
 *
 * 和 `useWS`（generic_chat 路径）状态机 / StrictMode 安全策略一致；
 * 上层在 Chat.tsx 按 `thread.backend_kind` 选用对应 hook，不要在同一组件里
 * 同时调两个 hook —— React rules of hooks。
 */
export function useClaudeCodeWS(threadId: string | undefined) {
  const [socket, setSocket] = useState<ClaudeCodeSocket | null>(null);
  const [state, setState] = useState<SocketState>("closed");
  const ref = useRef<ClaudeCodeSocket | null>(null);

  useEffect(() => {
    if (!threadId) {
      setSocket(null);
      setState("closed");
      return;
    }
    const s = new ClaudeCodeSocket(threadId);
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
