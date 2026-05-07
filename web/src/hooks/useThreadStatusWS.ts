import { useEffect, useRef } from "react";
import { useThreadStatusStore } from "@/stores/threadStatus";
import type { ThreadStatusFrame } from "@/protocol";

export function useThreadStatusWS() {
  const setStatus = useThreadStatusStore((s) => s.setStatus);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws/thread-status`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onmessage = (ev) => {
      try {
        const frame: ThreadStatusFrame = JSON.parse(ev.data);
        if (frame.type === "thread-status" && frame.threadId && frame.phase) {
          setStatus(frame.threadId, frame.phase, frame.toolName ?? undefined);
        }
      } catch {
        // 忽略非 JSON 帧
      }
    };

    ws.onclose = () => {
      // 可选：断线重连（先不做，留 P2）
    };

    return () => {
      wsRef.current = null;
      ws.close();
    };
  }, [setStatus]);
}
