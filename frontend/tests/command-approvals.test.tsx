import { describe, it, expect, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TelemetryDetail } from "../src/run/TelemetryDetail";
import { RunInspector } from "../src/run/RunInspector";
import { createApiClient, type FetchJSON, type WorkflowsApi } from "../src/api/client";
import { desktopClient } from "../src/desktop/api";
import type { PluginContext } from "@hermes/plugin-sdk";
import type { NodeTelemetryApproval, RunState, SpecDetail } from "../src/api/types";

const approval: NodeTelemetryApproval = { state: "pending", request_id: "prompt-1", can_respond: true,
  command: "fixture command", description: "confirm", choices: ["once", "deny"] };

describe("Command approvals", () => {
  it("binds web and Desktop writes to the exact attempt and prompt", async () => {
    const fetchJSON = vi.fn(async () => ({}));
    await createApiClient(fetchJSON as FetchJSON).decideCommand("r/a", "work", "attempt-2", "prompt/a", "once");
    expect(fetchJSON).toHaveBeenCalledWith("/api/plugins/hermes-workflows/runs/r%2Fa/approvals/prompt%2Fa", expect.objectContaining({
      method: "POST", body: JSON.stringify({ node_id: "work", attempt_id: "attempt-2", choice: "once" }),
    }));
    let scope = { connectionId: "local", profile: "qa" };
    const rest = vi.fn(async () => ({}));
    const client = desktopClient(rest as PluginContext["rest"], { modelOptions: async () => ({}), skills: async () => [] }, scope, () => scope, () => true);
    await client.decideCommand("run", "work", "att", "prompt", "deny");
    expect(rest).toHaveBeenCalledWith("/runs/run/approvals/prompt", {
      method: "POST", body: { node_id: "work", attempt_id: "att", choice: "deny" }, timeoutMs: 30000,
    });
    scope = { ...scope, profile: "another" };
    await expect(client.decideCommand("run", "work", "att", "prompt", "once")).rejects.toThrow("profile changed");
    expect(rest).toHaveBeenCalledTimes(1);
  });

  it("offers only native allowed choices and prevents duplicate clicks", async () => {
    let resolve!: () => void;
    const decide = vi.fn(() => new Promise<void>((done) => { resolve = done; }));
    render(<TelemetryDetail telemetry={{ approval }} nodeActive onDecision={decide} />);
    await userEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(decide).toHaveBeenCalledWith("prompt-1", "once");
    expect(screen.getByRole("button", { name: "Allow once" })).toBeDisabled();
    await act(async () => { resolve(); });
    await screen.findByText("Decision sent. Waiting for the worker.");
    expect(screen.queryByRole("button", { name: /always|session/i })).not.toBeInTheDocument();
  });

  it("keeps native-only prompts read-only and hides controls after cancellation", () => {
    const decide = vi.fn();
    const { rerender } = render(<TelemetryDetail telemetry={{ approval: { ...approval, can_respond: false } }} nodeActive onDecision={decide} />);
    expect(screen.getByText(/worker's chat\/CLI/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    rerender(<TelemetryDetail telemetry={{ approval }} nodeActive={false} onDecision={decide} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("does not carry an old prompt's late error into its replacement", async () => {
    let reject!: (error: Error) => void;
    const decide = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
    const { rerender } = render(<TelemetryDetail telemetry={{ approval }} nodeActive onDecision={decide} />);
    await userEvent.click(screen.getByRole("button", { name: "Deny command" }));
    rerender(<TelemetryDetail telemetry={{ approval: { ...approval, request_id: "prompt-2" } }} nodeActive onDecision={decide} />);
    await act(async () => { reject(new Error("stale request")); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Allow once" })).toBeEnabled());
    expect(screen.queryByText("stale request")).not.toBeInTheDocument();
  });

  it("shows failures and exposes each concurrent card's prompt", async () => {
    const decide = vi.fn(async () => { throw new Error("no longer pending"); });
    render(<TelemetryDetail telemetry={{ approvals: [approval, { ...approval, request_id: "prompt-2", command: "another", choices: ["deny"] }] }} nodeActive onDecision={decide} />);
    expect(screen.getAllByRole("button", { name: "Deny command" })).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no longer pending");
  });

  it("sends the run inspector's displayed attempt identity", async () => {
    const state: RunState = { run_id: "r", workflow_id: "wf", workflow_version: 1, status: "running",
      nodes: { work: { node_id: "work", status: "running", attempt_id: "attempt-4", telemetry: { approval } } } };
    const detail: SpecDetail = { path: "/wf.json", workflow: { id: "wf", name: "Test", version: 1,
      scope: { type: "global" }, trigger: { type: "manual" }, nodes: [{ id: "work", type: "agent_task", prompt: "capture", profile: "default" }], edges: [] } };
    const decideCommand = vi.fn(async () => ({}));
    const client = { getRun: async () => state, getWorkflow: async () => detail, decideCommand } as unknown as WorkflowsApi;
    render(<RunInspector runId="r" client={client} pollMs={10000} />);
    const button = await waitFor(() => {
      const element = document.querySelector('[aria-label="Open node work"]');
      if (!element) throw new Error("node missing");
      return element;
    });
    fireEvent.click(button);
    await userEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(decideCommand).toHaveBeenCalledWith("r", "work", "attempt-4", "prompt-1", "once");
  });
});
