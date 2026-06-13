import { useEffect, useState } from "react";
import { getChatLayoutState, type ChatLayoutState } from "@/lib/chat-layout";

const DEFAULT_LAYOUT: ChatLayoutState = {
  isMobileLayout: false,
  isCompactLayout: false,
  shouldOpenWhiteboard: true,
};

export function useChatLayout(): ChatLayoutState {
  const [layout, setLayout] = useState<ChatLayoutState>(() => {
    if (typeof window === "undefined") return DEFAULT_LAYOUT;
    return getChatLayoutState(window.innerWidth);
  });

  useEffect(() => {
    const sync = () => {
      setLayout(getChatLayoutState(window.innerWidth));
    };

    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  return layout;
}
