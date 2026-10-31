// The small public SDK surface this plugin uses, verified against the installed
// Hermes apps/desktop/src/sdk/index.ts and contrib/plugin.ts. Runtime external.
declare module "@hermes/plugin-sdk" {
  import type { ReactNode } from "react";
  export interface Readable<T> { get(): T; subscribe(callback: () => void): () => void }
  export interface PluginContext {
    rest<T>(path: string, options?: { method?: string; body?: unknown; timeoutMs?: number }): Promise<T>;
    onDispose(callback: () => void): void;
    registerMany(items: { id: string; area: string; order?: number; data?: unknown; render?: () => ReactNode }[]): () => void;
    os: { openExternal(url: string): unknown };
  }
  export interface HermesPlugin { id: string; name: string; description: string; defaultEnabled?: boolean; register(ctx: PluginContext): void }
  export const host: {
    state: { profile: Readable<string>; connectionId: Readable<string | null>; gateway: Readable<string> };
    request<T>(method: string, params?: Record<string, unknown>): Promise<T>;
    skills: { list(scope?: { connectionId: string; profile: string }): Promise<{ name?: string }[]> };
    navigate(path: string): void;
  };
  export function useValue<T>(store: Readable<T>): T;
  export const ROUTES_AREA: string;
  export const SIDEBAR_NAV_AREA: string;
  export const PALETTE_AREA: string;
}
declare const __HWP_DESKTOP_CSS__: string;
