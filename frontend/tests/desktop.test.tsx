import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { host } from "./desktop-sdk-mock";
import plugin, { DesktopPage } from "../src/desktop/plugin";
import { desktopClient } from "../src/desktop/api";
import type { PluginContext } from "@hermes/plugin-sdk";

const scope = { connectionId: "local", profile: "default" };
const catalog = { modelOptions: vi.fn(async () => ({ providers: [] })), skills: vi.fn(async () => [{ name: "test-skill" }]) };
const context = (rest = vi.fn(async () => ({}))) => ({ rest, registerMany: vi.fn(), onDispose: vi.fn(), os: { openExternal: vi.fn() } } as unknown as PluginContext);
beforeEach(() => { host.state.profile.set("default"); host.state.connectionId.set("local"); host.state.gateway.set("open"); vi.clearAllMocks(); });

describe("Desktop client", () => {
  it("uses only SDK REST with JSON objects and namespace-relative URLs", async () => {
    const rest = vi.fn(async () => ({}));
    const api = desktopClient(context(rest).rest, catalog, scope, () => scope, () => true);
    await api.reviewRun("run/a", "gate", "approved", "OK");
    expect(rest).toHaveBeenCalledWith("/runs/run%2Fa/review", { method: "POST", body: { node_id: "gate", decision: "approved", note: "OK" }, timeoutMs: 30000 });
    await api.listModels(); await api.listSkills();
    expect(catalog.skills).toHaveBeenCalledWith(scope);
    expect(rest).toHaveBeenCalledTimes(1);
  });
  it("rejects a stale mutation before it can reach another profile or gateway", async () => {
    const rest = vi.fn(async () => ({})); let owner = scope;
    const api = desktopClient(context(rest).rest, catalog, scope, () => owner, () => true);
    owner = { ...scope, profile: "other" };
    await expect(api.cancelRun("r")).rejects.toThrow("profile changed");
    owner = { ...scope, connectionId: "remote" };
    await expect(api.runWorkflow("test")).rejects.toThrow("profile changed");
    expect(rest).not.toHaveBeenCalled();
  });
  it("rejects a late response after the selected scope changes", async () => {
    let finish!: (value: unknown) => void; let owner = scope;
    const rest = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const api = desktopClient(context(rest).rest, catalog, scope, () => owner, () => true);
    const result = api.listWorkflows(); owner = { ...scope, profile: "other" }; finish({ workflows: [] });
    await expect(result).rejects.toThrow("profile changed");
  });
  it("refuses calls after disable or while disconnected", async () => {
    const rest = vi.fn(async () => ({})); let available = true;
    const api = desktopClient(context(rest).rest, catalog, scope, () => scope, () => available);
    available = false;
    await expect(api.deleteWorkflow("test")).rejects.toThrow("gateway or profile changed");
    expect(rest).not.toHaveBeenCalled();
  });
});

describe("Desktop integration", () => {
  it("registers route/nav/palette and removes its styles on disable", () => {
    const ctx = context(); const cleanups: (() => void)[] = [];
    ctx.onDispose = (fn) => { cleanups.push(fn); };
    plugin.register(ctx);
    expect(plugin.id).toBe("hermes-workflows"); expect(plugin.defaultEnabled).toBe(false);
    expect(ctx.registerMany).toHaveBeenCalledWith(expect.arrayContaining([
      expect.objectContaining({ area: "routes", data: { path: "/workflows" } }),
      expect.objectContaining({ area: "sidebar.nav", data: expect.objectContaining({ label: "Workflows" }) }),
    ]));
    const style = Array.from(document.head.querySelectorAll("style")).find((item) => item.textContent?.includes("hwp-desktop"));
    expect(style).toBeDefined(); cleanups.forEach((fn) => fn()); expect(style?.isConnected).toBe(false);
  });
  it("uses memory navigation without changing the Desktop URL", async () => {
    window.history.replaceState(null, "", "#/desktop-workspace"); const url = window.location.href;
    const rest = vi.fn(async (path: string) => path === "/workflows" ? { workflows: [] } : path === "/schedules" ? { schedules: [] } : {});
    render(<DesktopPage ctx={context(rest)} alive={() => true} />);
    await screen.findByRole("heading", { name: "Workflows", level: 2 });
    await userEvent.click(screen.getByRole("button", { name: "Schedules" }));
    await screen.findByText(/No schedules yet/);
    await userEvent.click(screen.getByRole("link", { name: "Workflows" }));
    await screen.findByRole("heading", { name: "Workflows", level: 2 });
    expect(window.location.href).toBe(url);
  });
  it("remounts the selected store and drops old UI on profile/connection change", async () => {
    const rest = vi.fn(async (path: string) => path === "/workflows" ? { workflows: [] } : path === "/schedules" ? { schedules: [] } : {});
    render(<DesktopPage ctx={context(rest)} alive={() => true} />);
    await screen.findByRole("heading", { name: "Workflows", level: 2 });
    await userEvent.click(screen.getByRole("button", { name: "Schedules" }));
    await screen.findByText(/No schedules yet/);
    act(() => host.state.profile.set("other"));
    await screen.findByRole("heading", { name: "Workflows", level: 2 });
    act(() => host.state.connectionId.set("remote"));
    await waitFor(() => expect(screen.getByText(/other · remote/)).toBeInTheDocument());
    act(() => host.state.gateway.set("connecting"));
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for the selected Hermes gateway");
  });
});
