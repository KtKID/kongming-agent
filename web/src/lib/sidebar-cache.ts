export interface CachedPayload<T> {
  savedAt: number;
  value: T;
}

function storageAvailable(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.localStorage !== "undefined" &&
    typeof window.localStorage.getItem === "function"
  );
}

export function loadCachedPayload<T>(key: string): CachedPayload<T> | null {
  if (!storageAvailable()) return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedPayload<T>;
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof parsed.savedAt !== "number" ||
      !("value" in parsed)
    ) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function saveCachedPayload<T>(key: string, value: T): void {
  if (!storageAvailable()) return;
  try {
    window.localStorage.setItem(
      key,
      JSON.stringify({
        savedAt: Date.now(),
        value,
      } satisfies CachedPayload<T>),
    );
  } catch {
    // Ignore quota/private-mode failures.
  }
}
