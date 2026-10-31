import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ActionDialog } from "../src/ui/components/ActionDialog";

describe("ActionDialog", () => {
  it("requires explicit confirmation and focuses Cancel for deletion", async () => {
    const onSubmit = vi.fn(async () => {});
    const onClose = vi.fn();
    render(<ActionDialog title="Delete schedule" description="Delete test schedule?" submitLabel="Delete" danger onSubmit={onSubmit} onClose={onClose} onSuccess={() => {}} />);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(onSubmit).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps invalid input open and does not submit", async () => {
    const onSubmit = vi.fn(async () => {});
    render(<ActionDialog title="Edit" description="Schedule" submitLabel="Save" fieldLabel="Cron" validate={(value) => value ? null : "Cron is required."} onSubmit={onSubmit} onClose={() => {}} onSuccess={() => {}} />);
    expect(screen.getByRole("textbox", { name: "Cron" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Cron is required.");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("shows backend errors and allows correction/retry", async () => {
    const onSubmit = vi.fn().mockRejectedValueOnce(new Error("Invalid cron range")).mockResolvedValueOnce({});
    const onSuccess = vi.fn();
    render(<ActionDialog title="Edit" description="Schedule" submitLabel="Save" fieldLabel="Cron" initialValue="99 * * * *" onSubmit={onSubmit} onClose={() => {}} onSuccess={onSuccess} />);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid cron range");
    expect(onSuccess).not.toHaveBeenCalled();
    await userEvent.clear(screen.getByRole("textbox", { name: "Cron" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Cron" }), " 0 0 * * * ");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    expect(onSubmit).toHaveBeenLastCalledWith("0 0 * * *");
  });

  it("blocks duplicate submission and dismissal while the request is pending", async () => {
    let finish!: () => void;
    const onSubmit = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const onClose = vi.fn(); const onSuccess = vi.fn();
    render(<ActionDialog title="Delete" description="Test" submitLabel="Confirm" onSubmit={onSubmit} onClose={onClose} onSuccess={onSuccess} />);
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await userEvent.keyboard("{Escape}{Tab}");
    expect(screen.getByRole("button", { name: "Close" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(onSubmit).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
    finish();
    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
  });

  it("keeps keyboard focus in the dialog and handles Escape", async () => {
    const onClose = vi.fn();
    render(<ActionDialog title="Edit" description="Test" submitLabel="Save" fieldLabel="Cron" onSubmit={async () => {}} onClose={onClose} onSuccess={() => {}} />);
    screen.getByRole("button", { name: "Save" }).focus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Save" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });
});
