import type { NodeTelemetry, NodeTelemetryApproval } from "../api/types";
import { useState } from "react";
import { Button } from "../ui/components";
import { errorMessage } from "./useRunPolling";

// Telemetry block for the run inspector's node detail: the
// observer-derived aggregates the worker recorded (duration, tokens, API and
// tool calls, subagents, structured error), plus the command-approval state —
// "waiting" while the node's worker is blocked on a human answer, and the
// deny/timeout context after the fact. Rows render only when the underlying
// counter exists, so a sparse aggregate stays compact.

export interface TelemetryDetailProps {
  telemetry: NodeTelemetry;
  /** Whether the node is still active (scheduled/running): a pending approval
   *  is announced only then — on a settled node it just means the worker died
   *  mid-prompt. */
  nodeActive?: boolean;
  onDecision?: (requestId: string, choice: "once" | "deny") => Promise<unknown>;
}

function formatDurationMs(ms: number): string {
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function formatTokens(t: NodeTelemetry): string {
  const total = t.total_tokens ?? 0;
  if (t.input_tokens !== undefined || t.output_tokens !== undefined) {
    return `${total} (${t.input_tokens ?? 0} in / ${t.output_tokens ?? 0} out)`;
  }
  return String(total);
}

function formatToolCalls(t: NodeTelemetry): string {
  const calls = String(t.tool_calls ?? 0);
  return t.tool_errors !== undefined && t.tool_errors > 0
    ? `${calls} (${t.tool_errors} failed)`
    : calls;
}

function ApprovalNote({
  approval,
  nodeActive,
  onDecision,
}: {
  approval: NodeTelemetryApproval;
  nodeActive: boolean;
  onDecision?: TelemetryDetailProps["onDecision"];
}): React.ReactElement | null {
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controlled = approval.can_respond === true && approval.request_id !== undefined && onDecision !== undefined;
  const decide = async (choice: "once" | "deny") => {
    if (!controlled || busy || sent) return;
    setBusy(true); setError(null);
    try {
      await onDecision(approval.request_id!, choice);
      setSent(true);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  if (approval.state === "pending" && nodeActive) {
    return (
      <div className="hw-approval">
        <p className="hw-approval__pending">
          {sent ? "Decision sent. Waiting for the worker." : controlled
            ? "Waiting for command approval."
            : "Waiting for command approval — answer it on the worker's chat/CLI surface."}
        </p>
        {approval.command !== undefined && <pre className="hw-output">{approval.command}</pre>}
        {approval.description !== undefined && <p className="hw-note">{approval.description}</p>}
        {controlled && <div className="hw-actions">
          {(approval.choices ?? []).includes("once") && <Button disabled={busy || sent} onClick={() => void decide("once")}>Allow once</Button>}
          {(approval.choices ?? []).includes("deny") && <Button disabled={busy || sent} onClick={() => void decide("deny")}>Deny command</Button>}
        </div>}
        {error !== null && <p className="hw-error" role="alert">{error}</p>}
      </div>
    );
  }
  if (approval.state === "resolved" && ["deny", "timeout", "cancelled"].includes(approval.choice ?? "")) {
    return (
      <div className="hw-approval">
        <p className="hw-error">
          {approval.choice === "deny" ? "Command approval denied" : approval.choice === "timeout" ? "Command approval timed out" : "Command approval cancelled"}
        </p>
        {approval.command !== undefined && <pre className="hw-output">{approval.command}</pre>}
      </div>
    );
  }
  return null;
}

export function TelemetryDetail({
  telemetry: t,
  nodeActive = false,
  onDecision,
}: TelemetryDetailProps): React.ReactElement {
  const rows: [string, string][] = [];
  if (t.duration_ms !== undefined) rows.push(["Duration", formatDurationMs(t.duration_ms)]);
  if (t.total_tokens !== undefined) rows.push(["Tokens", formatTokens(t)]);
  if (t.api_calls !== undefined) rows.push(["API calls", String(t.api_calls)]);
  if (t.tool_calls !== undefined) rows.push(["Tool calls", formatToolCalls(t)]);
  if (t.subagents !== undefined) rows.push(["Subagents", String(t.subagents)]);

  return (
    <div className="hw-telemetry">
      <div className="hw-eyebrow">Agent telemetry</div>
      {rows.map(([label, value]) => (
        <div key={label} className="hw-telemetry-row">
          <span className="hw-telemetry-label">{label}</span>
          <span>{value}</span>
        </div>
      ))}
      {t.error_type !== undefined && (
        <p className="hw-error">
          {t.error_message !== undefined ? `${t.error_type}: ${t.error_message}` : t.error_type}
        </p>
      )}
      {(t.approvals ?? (t.approval ? [t.approval] : [])).map((approval, index) => (
        <ApprovalNote key={approval.request_id ?? `${approval.requested_at}:${index}`} approval={approval} nodeActive={nodeActive} onDecision={onDecision} />
      ))}
    </div>
  );
}
