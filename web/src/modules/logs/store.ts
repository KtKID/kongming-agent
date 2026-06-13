/**
 * Log viewer Zustand store for full-log v0.2.
 *
 * The store holds UI state only. Components call module APIs explicitly for
 * loading and refreshing.
 */

import { create } from "zustand";
import type { LogSource, LogLine } from "./types";

export interface LogViewerState {
  isOpen: boolean;
  open: () => void;
  close: () => void;

  sources: LogSource[];
  selectedType: string | null;
  lines: LogLine[];

  loadingSources: boolean;
  loadingContent: boolean;
  error: string | null;

  tailLines: number;
  query: string;
  truncated: boolean;
  readBytes: number;
  totalBytes: number | null;
  lastLoadedAt: number | null;

  setSources: (sources: LogSource[]) => void;
  setSelectedType: (type: string) => void;
  setLines: (lines: LogLine[]) => void;
  setLoadingSources: (loading: boolean) => void;
  setLoadingContent: (loading: boolean) => void;
  setError: (error: string | null) => void;
  setTruncated: (truncated: boolean) => void;
  setReadMeta: (readBytes: number, totalBytes: number | null) => void;
  setLastLoadedAt: (ts: number | null) => void;
  setQuery: (query: string) => void;
  reset: () => void;
}

const INITIAL_STATE: Omit<
  LogViewerState,
  | "open"
  | "close"
  | "setSources"
  | "setSelectedType"
  | "setLines"
  | "setLoadingSources"
  | "setLoadingContent"
  | "setError"
  | "setTruncated"
  | "setReadMeta"
  | "setLastLoadedAt"
  | "setQuery"
  | "reset"
> = {
  isOpen: false,
  sources: [],
  selectedType: null,
  lines: [],
  loadingSources: false,
  loadingContent: false,
  error: null,
  tailLines: 200,
  query: "",
  truncated: false,
  readBytes: 0,
  totalBytes: null,
  lastLoadedAt: null,
};

export const useLogViewerStore = create<LogViewerState>((set) => ({
  ...INITIAL_STATE,

  open: () => set({ isOpen: true }),
  close: () => set({ isOpen: false }),

  setSources: (sources) => set({ sources }),
  setSelectedType: (selectedType) => set({ selectedType }),
  setLines: (lines) => set({ lines }),
  setLoadingSources: (loadingSources) => set({ loadingSources }),
  setLoadingContent: (loadingContent) => set({ loadingContent }),
  setError: (error) => set({ error }),
  setTruncated: (truncated) => set({ truncated }),
  setReadMeta: (readBytes, totalBytes) => set({ readBytes, totalBytes }),
  setLastLoadedAt: (lastLoadedAt) => set({ lastLoadedAt }),
  setQuery: (query) => set({ query }),

  reset: () => set({ ...INITIAL_STATE }),
}));
