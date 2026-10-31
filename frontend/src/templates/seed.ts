// Build the minimal valid workflow a freshly-created spec starts from. A lone
// `finish` node with no edges validates cleanly: there is no agent_task to
// require a profile, and the single node is its own reachable entry. The user
// then grows the graph in the editor. Pure — no I/O.
import type { Scope, Trigger, Workflow } from "../api/types";

export interface SeedForm {
  id: string;
  name: string;
  scope: Scope;
  trigger: Trigger;
}

export function buildSeedWorkflow(form: SeedForm): Workflow {
  return {
    id: form.id,
    name: form.name,
    version: 1,
    scope: form.scope,
    trigger: form.trigger,
    nodes: [{ id: "finish", type: "finish", outcome: "success" }],
    edges: [],
  };
}
