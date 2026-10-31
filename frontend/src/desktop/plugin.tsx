import { useMemo, type MouseEvent } from "react";
import { host, useValue, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, type HermesPlugin, type PluginContext } from "@hermes/plugin-sdk";
import { App } from "../App";
import { desktopClient, scopeKey, type DesktopScope } from "./api";
import "./style.css";

function currentScope(): DesktopScope {
  return { connectionId: host.state.connectionId.get() || "local", profile: host.state.profile.get() || "default" };
}

export function DesktopPage({ ctx, alive }: { ctx: PluginContext; alive: () => boolean }): React.ReactElement {
  const profile = useValue(host.state.profile) || "default";
  const connectionId = useValue(host.state.connectionId) || "local";
  const gateway = useValue(host.state.gateway);
  const scope = useMemo(() => ({ connectionId, profile }), [connectionId, profile]);
  const client = useMemo(() => desktopClient(ctx.rest, {
    modelOptions: () => host.request("model.options", { explicit_only: true }),
    skills: (owner) => host.skills.list(owner),
  }, scope, currentScope, () => alive() && host.state.gateway.get() === "open"), [ctx, scope, alive]);

  const links = (event: MouseEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || !(event.target instanceof Element)) return;
    const link = event.target.closest("a");
    const href = link?.getAttribute("href");
    if (!href) return;
    if (href.startsWith("#")) event.preventDefault(); // Never overwrite Desktop's router.
    else if (href === "/plugins") { event.preventDefault(); host.navigate("/capabilities?tab=plugins"); }
    else if (/^https?:\/\//.test(href)) { event.preventDefault(); ctx.os.openExternal(href); }
  };
  return <div className="hwp-desktop" onClick={links} onAuxClick={links}>
    <p className="hwp-desktop-scope">Workflows · {profile} · {connectionId}</p>
    {gateway !== "open" ? <p role="status">Waiting for the selected Hermes gateway…</p>
      : <App key={scopeKey(scope)} client={client} navigation="memory" reviewControls />}
  </div>;
}

const plugin: HermesPlugin = {
  id: "hermes-workflows", name: "Workflows",
  description: "Visual workflows for the native Python backend: edit, run, review, and schedule.",
  defaultEnabled: false,
  register(ctx) {
    let disposed = false;
    const alive = () => !disposed;
    const style = document.createElement("style");
    style.textContent = __HWP_DESKTOP_CSS__;
    document.head.appendChild(style);
    ctx.onDispose(() => { disposed = true; style.remove(); });
    ctx.registerMany([
      { id: "page", area: ROUTES_AREA, data: { path: "/workflows" }, render: () => <DesktopPage ctx={ctx} alive={alive} /> },
      { id: "nav", area: SIDEBAR_NAV_AREA, order: 55, data: { codicon: "git-merge", label: "Workflows", path: "/workflows" } },
      { id: "open", area: PALETTE_AREA, data: { id: "hermes-workflows.open", label: "Open Workflows", keywords: ["workflow", "DAG", "review"], run: () => host.navigate("/workflows") } },
    ]);
  },
};
export default plugin;
