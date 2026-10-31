import { describe, it, expect, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunReviews } from "../src/run/RunReviews";
import type { WorkflowsApi } from "../src/api/client";

function client(reviewRun: WorkflowsApi["reviewRun"]) {
  return { getRun: vi.fn(async () => ({ status: "waiting", nodes: { gate: {
    node_id: "gate", status: "waiting_for_review", review_options: ["approved"],
  } } })), reviewRun } as unknown as WorkflowsApi;
}
describe("native review controls", () => {
  it("keeps the new attempt visible when the preceding approval response arrives late", async () => {
    vi.useFakeTimers();
    try {
      let attempt = "first";
      let finish!: () => void;
      const review = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
      const api = { getRun: vi.fn(async () => ({ status: "waiting", nodes: { gate: {
        node_id: "gate", status: "waiting_for_review", attempt_id: attempt, review_options: ["approved"],
      } } })), reviewRun: review } as unknown as WorkflowsApi;
      const view = render(<RunReviews runId="loop-run" client={api} />);
      await act(async () => { await Promise.resolve(); });
      fireEvent.click(screen.getByRole("button", { name: "Approve" }));
      expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
      attempt = "second";
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(screen.getByRole("button", { name: "Approve" })).not.toBeDisabled();
      await act(async () => { finish(); });
      expect(screen.getByRole("button", { name: "Approve" })).not.toBeDisabled();
      view.unmount();
    } finally { vi.useRealTimers(); }
  });
  it("does not hide another run's gate when an earlier approval response arrives late", async () => {
    let finish!: () => void;
    const review = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const api = client(review);
    const view = render(<RunReviews runId="old-run" client={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    view.rerender(<RunReviews runId="new-run" client={api} />);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect(approve).not.toBeDisabled();
    await act(async () => { finish(); });
    expect(screen.getByRole("button", { name: "Approve" })).not.toBeDisabled();
  });
  it("shows a new loop attempt even when polling never saw the intermediate transition", async () => {
    vi.useFakeTimers();
    try {
      let attempt = "first-attempt";
      const review = vi.fn().mockResolvedValue({});
      const api = { getRun: vi.fn(async () => ({ status: "waiting", nodes: { gate: {
        node_id: "gate", status: "waiting_for_review", attempt_id: attempt, review_options: ["approved"],
      } } })), reviewRun: review } as unknown as WorkflowsApi;
      const view = render(<RunReviews runId="loop-run" client={api} />);
      await act(async () => { await Promise.resolve(); });
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "first note" } });
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Approve" })); });
      expect(review).toHaveBeenLastCalledWith("loop-run", "gate", "approved", "first note", "first-attempt");
      expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
      attempt = "second-attempt";
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(screen.getByRole("textbox")).toHaveValue("");
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Approve" })); });
      expect(review).toHaveBeenLastCalledWith("loop-run", "gate", "approved", undefined, "second-attempt");
      view.unmount();
    } finally { vi.useRealTimers(); }
  });
  it("honors allowed decisions and submits the optional note once while busy", async () => {
    let finish!: () => void;
    const review = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(<RunReviews runId="test-1" client={client(review)} />);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox"), " Checked ");
    await userEvent.dblClick(approve);
    expect(review).toHaveBeenCalledTimes(1);
    expect(review).toHaveBeenCalledWith("test-1", "gate", "approved", "Checked");
    expect(approve).toBeDisabled(); finish();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument());
  });
  it("keeps a failed review available for retry", async () => {
    const review = vi.fn().mockRejectedValueOnce(new Error("Review unavailable")).mockResolvedValue({});
    render(<RunReviews runId="test-1" client={client(review)} />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Review unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(review).toHaveBeenCalledTimes(2));
  });
});
