import { useEffect, useRef, useState } from "react";
import { ThreadSocket, type SocketState } from "@/lib/ws";

/**
 * 给定 threadId，建一个长连接；返回 ThreadSocket 实例 + 当前状态。
 *
 * StrictMode 双 mount 安全：
 * - useEffect 的 cleanup 会被立即触发 + 重新创建（dev StrictMode）
 * - ThreadSocket.close() 设 disposed=true，幂等
 * - 重新 mount 时新建实例（旧的已 disposed，不会有 race）
 *
 * threadId 切换时：
 * - cleanup 旧实例 → 创建新实例
 */
export function useWS(threadId: string | undefined) {
  const [socket, setSocket] = useState<ThreadSocket | null>(null);
  const [state, setState] = useState<SocketState>("closed");
  // 防止 cleanup race：用 ref 持当前实例，确保 close 的就是它
  const ref = useRef<ThreadSocket | null>(null);

  useEffect(() => {
    if (!threadId) {
      setSocket(null);
      setState("closed");
      return;
    }
    const s = new ThreadSocket(threadId);
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
