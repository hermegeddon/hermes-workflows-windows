import { useSyncExternalStore } from "react";
import { vi } from "vitest";
function atom<T>(initial: T) {
  let value = initial;
  const listeners = new Set<() => void>();
  return { get: () => value, set: (next: T) => { value = next; listeners.forEach((listener) => listener()); }, subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; } };
}
export const host = {
  state: { profile: atom("default"), connectionId: atom<string | null>("local"), gateway: atom("open") },
  request: vi.fn(async () => ({ providers: [] })),
  skills: { list: vi.fn(async () => []) }, navigate: vi.fn(),
};
export const useValue = <T,>(store: { get(): T; subscribe(listener: () => void): () => void }): T => useSyncExternalStore(store.subscribe, store.get);
export const ROUTES_AREA = "routes";
export const SIDEBAR_NAV_AREA = "sidebar.nav";
export const PALETTE_AREA = "palette";
