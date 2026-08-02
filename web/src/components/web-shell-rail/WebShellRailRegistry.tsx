import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { WebShellRailItem } from "./types";

interface WebShellRailRegistryValue {
  items: WebShellRailItem[];
  registerItems: (sourceId: string, items: WebShellRailItem[]) => () => void;
}

interface WebShellRailRegistration {
  token: symbol;
  items: WebShellRailItem[];
}

const WebShellRailRegistryContext =
  createContext<WebShellRailRegistryValue | null>(null);

export function WebShellRailProvider({ children }: { children: ReactNode }) {
  const [itemsBySource, setItemsBySource] = useState<
    Record<string, WebShellRailRegistration>
  >({});

  const registerItems = useCallback(
    (sourceId: string, items: WebShellRailItem[]) => {
      const token = Symbol(sourceId);
      setItemsBySource((current) => {
        if (current[sourceId]) {
          console.warn(`WebShellRail source already registered: ${sourceId}`);
        }
        return { ...current, [sourceId]: { token, items } };
      });
      return () => {
        setItemsBySource((current) => {
          if (current[sourceId]?.token !== token) return current;
          const next = { ...current };
          delete next[sourceId];
          return next;
        });
      };
    },
    [],
  );

  const value = useMemo<WebShellRailRegistryValue>(
    () => ({
      items: Object.values(itemsBySource).flatMap((registration) => registration.items),
      registerItems,
    }),
    [itemsBySource, registerItems],
  );

  return (
    <WebShellRailRegistryContext.Provider value={value}>
      {children}
    </WebShellRailRegistryContext.Provider>
  );
}

export function useWebShellRailRegisteredItems(): WebShellRailItem[] {
  const registry = useContext(WebShellRailRegistryContext);
  return registry?.items ?? [];
}

export function useRegisterWebShellRailItems(
  sourceId: string,
  items: WebShellRailItem[],
): void {
  const registry = useContext(WebShellRailRegistryContext);
  const registerItems = registry?.registerItems;
  useEffect(() => {
    if (!registerItems) return undefined;
    return registerItems(sourceId, items);
  }, [items, registerItems, sourceId]);
}
