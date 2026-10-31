import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { BackendUnavailable } from "../src/ui/components/BackendUnavailable";

describe("BackendUnavailable", () => {
  it("explains selected-owner Python setup and links to this plugin's guide", () => {
    render(<BackendUnavailable resource="workflows" />);
    expect(screen.getByRole("heading", { name: "Could not load workflows" })).toBeInTheDocument();
    expect(screen.getByText(/the request for workflows failed/i)).toBeInTheDocument();
    expect(screen.getByText(/installed and enabled for that gateway and profile/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Plugins" })).toHaveAttribute("href", "/plugins");
    expect(screen.getByRole("link", { name: "Python plugin setup guide" })).toHaveAttribute("href", expect.stringContaining("hermegeddon/hermes-workflows-windows"));
    expect(document.body.textContent).not.toMatch(/systemd|launchd|hermes-workflows-dashboard-api|GHSA-/);
  });
  it("keeps a reverse-proxy prefix on the web plugin settings link", () => {
    window.__HERMES_BASE_PATH__ = "/hermes";
    try {
      render(<BackendUnavailable resource="runs" />);
      expect(screen.getByRole("link", { name: "Plugins" })).toHaveAttribute("href", "/hermes/plugins");
    } finally { delete window.__HERMES_BASE_PATH__; }
  });
  it("preserves the actual server error and omits missing error details", () => {
    const { rerender } = render(<BackendUnavailable resource="runs" detail={'404: {"detail":"Plugin not found"}'} />);
    expect(screen.getByText(/Plugin not found/)).toBeInTheDocument();
    rerender(<BackendUnavailable resource="schedules" />);
    expect(screen.queryByText(/Backend error:/)).not.toBeInTheDocument();
  });
});
