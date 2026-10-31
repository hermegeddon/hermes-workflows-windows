import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunReviews } from "../src/run/RunReviews";
import type { WorkflowsApi } from "../src/api/client";

function client(reviewRun: WorkflowsApi["reviewRun"]) {
  return { getRun: vi.fn(async () => ({ status: "waiting", nodes: { gate: {
    node_id: "gate", status: "waiting_for_review", review_options: ["approved"],
  } } })), reviewRun } as unknown as WorkflowsApi;
}
describe("native review controls", () => {
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
