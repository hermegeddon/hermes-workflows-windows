import type { PluginContext } from "@hermes/plugin-sdk";
import { createApiClient, type FetchJSON, type WorkflowsApi } from "../api/client";

export interface DesktopScope { connectionId: string; profile: string }
export interface DesktopCatalog {
  modelOptions(): Promise<unknown>;
  skills(scope: DesktopScope): Promise<unknown>;
}
export function scopeKey(scope: DesktopScope): string { return JSON.stringify([scope.connectionId, scope.profile]); }

/** No cookies, addresses, or credentials leave Desktop's SDK transport. */
export function desktopClient(
  rest: PluginContext["rest"], catalog: DesktopCatalog,
  expected: DesktopScope, current: () => DesktopScope, available: () => boolean,
): WorkflowsApi {
  const assertScope = () => {
    if (!available() || scopeKey(current()) !== scopeKey(expected)) {
      throw new Error("The gateway or profile changed. Reopen Workflows before trying again.");
    }
  };
  const fetchJSON: FetchJSON = async <T>(path: string, init?: RequestInit): Promise<T> => {
    assertScope();
    let result: unknown;
    if (path === "/api/model/options") result = await catalog.modelOptions();
    else if (path === "/api/skills") result = await catalog.skills(expected);
    else {
      const prefix = "/api/plugins/hermes-workflows";
      if (!path.startsWith(prefix + "/")) throw new Error("Unsupported Workflows API path.");
      if (init?.body !== undefined && typeof init.body !== "string") throw new Error("Expected a JSON request body.");
      result = await rest(path.slice(prefix.length), {
        method: init?.method ?? "GET",
        ...(init?.body ? { body: JSON.parse(init.body as string) as unknown } : {}),
        timeoutMs: 30_000,
      });
    }
    // An old read must never hydrate the newly selected profile's UI.
    assertScope();
    return result as T;
  };
  return createApiClient(fetchJSON);
}
