/**
 * Pure graph helpers shared by validation, the compiler, and the advance engine.
 * No I/O, no mutation of inputs.
 */

import type { Edge, Workflow } from "./workflow.ts";
import type { WorkflowNode } from "./nodes.ts";

export function nodeMap(workflow: Workflow): Map<string, WorkflowNode> {
  return new Map(workflow.nodes.map((node) => [node.id, node]));
}

export function outgoingEdges(workflow: Workflow, nodeId: string): Edge[] {
  return workflow.edges.filter((edge) => edge.from === nodeId);
}

export function incomingEdges(workflow: Workflow, nodeId: string): Edge[] {
  return workflow.edges.filter((edge) => edge.to === nodeId);
}

/** Entry nodes have no incoming edge. A valid workflow has exactly one. */
export function entryNodes(workflow: Workflow): WorkflowNode[] {
  const hasIncoming = new Set(workflow.edges.map((edge) => edge.to));
  return workflow.nodes.filter((node) => !hasIncoming.has(node.id));
}

/** Set of node ids reachable from `start` by following edges (ignores conditions). */
export function reachableFrom(workflow: Workflow, start: string): Set<string> {
  const seen = new Set<string>();
  const queue: string[] = [start];
  while (queue.length > 0) {
    const current = queue.shift() as string;
    if (seen.has(current)) continue;
    seen.add(current);
    for (const edge of outgoingEdges(workflow, current)) {
      if (!seen.has(edge.to)) queue.push(edge.to);
    }
  }
  return seen;
}
