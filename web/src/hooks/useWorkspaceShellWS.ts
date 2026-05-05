import { useEffect, useRef, useState } from "react";
import {
  WorkspaceShellSocket,
  type SocketState,
} from "@/lib/workspace-shell";

export function useWorkspaceShellWS(threadId: string | undefined) {
  const [socket, setSocket] = useState<WorkspaceShellSocket | null>(null);
  const [state, setState] = useState<SocketState>("closed");
  const ref = useRef<WorkspaceShellSocket | null>(null);

  useEffect(() => {
    if (!threadId) {
      setSocket(null);
      setState("closed");
      return;
    }
    const instance = new WorkspaceShellSocket(threadId);
    ref.current = instance;
    setSocket(instance);
    const offState = instance.onState((next) => setState(next));
    instance.connect();
    return () => {
      offState();
      instance.close();
      if (ref.current === instance) ref.current = null;
    };
  }, [threadId]);

  return { socket, state };
}
