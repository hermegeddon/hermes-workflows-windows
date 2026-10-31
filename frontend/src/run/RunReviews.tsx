import { useEffect, useRef, useState } from "react";
import type { WorkflowsApi } from "../api/client";
import { Button, Field, Textarea } from "../ui/components";

interface Gate { node_id: string; status: string; review_options?: string[]; attempt_id?: string }
const gateKey = (node: Gate) => `${node.node_id}:${node.attempt_id ?? "legacy"}`;

export function RunReviews({ runId, client }: { runId: string; client: WorkflowsApi }): React.ReactElement {
  const [nodes, setNodes] = useState<Gate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const resolved = useRef(new Set<string>());
  const owner = useRef({ client, runId });
  owner.current = { client, runId };
  useEffect(() => {
    resolved.current.clear(); setNodes([]); setError(null);
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const run = await client.getRun(runId);
        if (!active) return;
        setError(null);
        const waiting = ["created", "running", "waiting"].includes(run.status)
          ? (Object.values(run.nodes) as Gate[]).filter((node) => node.status === "waiting_for_review") : [];
        // Forget acknowledgements once the server has observed the transition,
        // so retrying this same run can expose the same gate again.
        for (const id of resolved.current) if (!waiting.some((node) => gateKey(node) === id)) resolved.current.delete(id);
        setNodes(waiting.filter((node) => !resolved.current.has(gateKey(node))));
      } catch (failure) {
        if (active) { setNodes([]); setError(failure instanceof Error ? failure.message : "Could not load reviews."); }
      }
      if (active) timer = setTimeout(poll, 1000);
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [client, runId]);
  return <div aria-live="polite">
    {error && <p role="alert" className="hw-alert">{error}</p>}
    {nodes.map((node) => <ReviewGate key={`${runId}:${gateKey(node)}`} runId={runId} node={node} client={client}
      onReviewed={() => {
        if (owner.current.client !== client || owner.current.runId !== runId) return;
        resolved.current.add(gateKey(node)); setNodes((current) => current.filter((item) => gateKey(item) !== gateKey(node)));
      }} />)}
  </div>;
}

function ReviewGate({ runId, node, client, onReviewed }: { runId: string; node: Gate; client: WorkflowsApi; onReviewed: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  async function decide(decision: string) {
    if (submitting.current) return;
    submitting.current = true; setBusy(true); setError(null);
    try {
      if (node.attempt_id) await client.reviewRun(runId, node.node_id, decision, note.trim() || undefined, node.attempt_id);
      else await client.reviewRun(runId, node.node_id, decision, note.trim() || undefined);
      onReviewed();
    }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Could not submit review."); }
    finally { submitting.current = false; setBusy(false); }
  }
  return <section className="hw-page hw-review" aria-label={`Review ${node.node_id}`}>
    <h2>Human review required</h2><p>Step: {node.node_id}</p>
    <Field label="Review note (optional)"><Textarea rows={2} value={note} disabled={busy} onChange={(event) => setNote(event.target.value)} /></Field>
    <div className="hw-actions">{[["approved", "Approve"], ["rejected", "Reject"], ["needs_changes", "Request changes"]]
      .filter(([value]) => !node.review_options || node.review_options.includes(value!))
      .map(([value, label]) => <Button key={value} disabled={busy} onClick={() => void decide(value!)}>{label}</Button>)}</div>
    {error && <p role="alert" className="hw-alert">{error}</p>}
  </section>;
}
