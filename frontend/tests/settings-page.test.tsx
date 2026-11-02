import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsPage } from "../src/pages/SettingsPage";
import type { WorkflowsApi } from "../src/api/client";
import type { SettingsResponse } from "../src/api/types";

const response: SettingsResponse = {
  values: { default_mode: "durable", max_parallel_runs: 4, fail_open: true, internal_board: "hermes-workflows" },
  schema: {
    namespace: "plugins.workflows",
    groups: [
      {
        key: "execution",
        label: "Execution",
        fields: [
          { key: "default_mode", type: "enum", enforced: true, default: "durable", options: ["durable", "direct"] },
          { key: "max_parallel_runs", type: "int", enforced: false, default: 4 },
        ],
      },
      {
        key: "open_second_brain",
        label: "OpenSecondBrain",
        fields: [
          { key: "fail_open", type: "bool", enforced: true, default: true },
          { key: "internal_board", type: "string", enforced: true, default: "hermes-workflows" },
        ],
      },
    ],
  },
};

function stubClient(overrides: Partial<WorkflowsApi> = {}): WorkflowsApi {
  const base = {
    getSettings: vi.fn(async () => response),
    saveSettings: vi.fn(async () => response),
  };
  return { ...base, ...overrides } as unknown as WorkflowsApi;
}

afterEach(() => vi.restoreAllMocks());

describe("SettingsPage", () => {
  const memoryResponse: SettingsResponse = {
    ...response,
    memory: {
      default_enabled: true, route: "hermes",
      host: { available: true, provider: "hindsight", consented: true, provider_support: "unprobed" },
      last_verified_capabilities: { write: true, lookup: false, idempotency: false },
      counts: { queued: 4 }, last_delivery: { run_id: "run-1", status: "queued" },
    },
  };

  it("shows the configured provider and queued receipt without claiming search visibility", async () => {
    render(<SettingsPage client={stubClient({ getSettings: vi.fn(async () => memoryResponse) })} />);
    expect(await screen.findByText(/Configured Hermes provider: hindsight/)).toBeInTheDocument();
    expect(screen.getByText(/Queued records await provider processing/)).toBeInTheDocument();
    expect(screen.getByText(/receipt lookup unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/connected|searchable|stored successfully/i)).not.toBeInTheDocument();
  });

  it("clearly degrades when the host has no memory writer", async () => {
    const older: SettingsResponse = { ...memoryResponse, memory: { ...memoryResponse.memory!,
      host: { available: false, provider: "hindsight", reason: "capability_unavailable" },
    } };
    render(<SettingsPage client={stubClient({ getSettings: vi.fn(async () => older) })} />);
    expect(await screen.findByText(/unavailable on this Hermes version/i)).toBeInTheDocument();
  });

  it("refreshes memory status without overwriting unsaved settings", async () => {
    const updated: SettingsResponse = { ...memoryResponse, memory: { ...memoryResponse.memory!,
      last_delivery: { run_id: "run-1", status: "acknowledged" },
    } };
    const getSettings = vi.fn().mockResolvedValueOnce(memoryResponse).mockResolvedValueOnce(updated);
    render(<SettingsPage client={stubClient({ getSettings })} />);
    const board = await screen.findByLabelText(/internal board/i);
    await userEvent.clear(board);
    await userEvent.type(board, "unsaved-edit");
    await userEvent.click(screen.getByRole("button", { name: /refresh memory status/i }));
    expect(await screen.findByText("acknowledged")).toBeInTheDocument();
    expect(board).toHaveValue("unsaved-edit");
  });

  it("uses descriptive native memory field labels when supplied by the backend", async () => {
    const native: SettingsResponse = { ...response, schema: { ...response.schema, groups: [{
      key: "memory", label: "Workflow memory", fields: [{
        key: "workflow_memory_enabled", label: "Store workflow memory by default",
        type: "bool", enforced: true, default: false,
      }],
    }] } };
    render(<SettingsPage client={stubClient({ getSettings: vi.fn(async () => native) })} />);
    expect(await screen.findByRole("switch", { name: "Store workflow memory by default" })).toBeInTheDocument();
  });

  it("renders every group and field from the schema", async () => {
    render(<SettingsPage client={stubClient()} />);
    expect(await screen.findByText("Execution")).toBeInTheDocument();
    expect(screen.getByText("OpenSecondBrain")).toBeInTheDocument();
    expect(screen.getByLabelText(/default mode/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/max parallel runs/i)).toBeInTheDocument();
    // A bool renders as a switch (role=switch), labelled by its inline text.
    expect(screen.getByRole("switch", { name: /fail open/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/internal board/i)).toBeInTheDocument();
  });

  it("labels knobs that are not yet enforced", async () => {
    render(<SettingsPage client={stubClient()} />);
    await screen.findByText("Execution");
    expect(screen.getByText(/not yet enforced/i)).toBeInTheDocument();
  });

  it("edits a field and posts the values on save", async () => {
    const saveSettings = vi.fn(async (_values: Record<string, unknown>) => response);
    render(<SettingsPage client={stubClient({ saveSettings })} />);

    const board = await screen.findByLabelText(/internal board/i);
    await userEvent.clear(board);
    await userEvent.type(board, "my-board");
    await userEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(saveSettings).toHaveBeenCalledTimes(1));
    const sent = saveSettings.mock.calls[0]![0];
    expect(sent.internal_board).toBe("my-board");
  });

  it("shows a saved confirmation after a successful save", async () => {
    render(<SettingsPage client={stubClient()} />);
    await screen.findByLabelText(/internal board/i);
    await userEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("renders a validation error inline when save is rejected", async () => {
    const saveSettings = vi.fn(async () => {
      throw new Error("'default_mode' must be one of ['durable', 'direct']");
    });
    render(<SettingsPage client={stubClient({ saveSettings })} />);
    await screen.findByLabelText(/internal board/i);
    await userEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText(/must be one of/i)).toBeInTheDocument();
  });

  it("surfaces a load error", async () => {
    const client = stubClient({
      getSettings: vi.fn(async () => {
        throw new Error("boom");
      }),
    });
    render(<SettingsPage client={client} />);
    expect(await screen.findByText(/Could not load settings/i)).toBeInTheDocument();
    expect(screen.getByText(/the request for settings failed/i)).toBeInTheDocument();
  });
});
