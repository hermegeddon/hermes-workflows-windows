/**
 * Optional visual-layout block for the dashboard editor. Strictly separated from
 * execution: a workflow is valid and runnable without any `ui`. Parsing is
 * lenient — malformed entries are dropped rather than raised — because layout is
 * cosmetic and must never block loading or running a spec.
 */

/** A single node's canvas position, keyed by the workflow node id. */
export interface XyflowNodeLayout {
  id: string;
  x: number;
  y: number;
}

/** The xyflow viewport (pan + zoom). */
export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

export interface XyflowLayout {
  nodes?: XyflowNodeLayout[];
  viewport?: Viewport;
}

export interface UiLayout {
  xyflow?: XyflowLayout;
}

type Rec = Record<string, unknown>;

function isRecord(value: unknown): value is Rec {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function parseNode(value: unknown): XyflowNodeLayout | null {
  if (!isRecord(value)) return null;
  const x = num(value["x"]);
  const y = num(value["y"]);
  if (typeof value["id"] !== "string" || x === null || y === null) return null;
  return { id: value["id"], x, y };
}

function parseViewport(value: unknown): Viewport | null {
  if (!isRecord(value)) return null;
  const x = num(value["x"]);
  const y = num(value["y"]);
  const zoom = num(value["zoom"]);
  if (x === null || y === null || zoom === null) return null;
  return { x, y, zoom };
}

function parseXyflow(value: unknown): XyflowLayout | null {
  if (!isRecord(value)) return null;
  const layout: XyflowLayout = {};
  if (Array.isArray(value["nodes"])) {
    const nodes = value["nodes"].map(parseNode).filter((n): n is XyflowNodeLayout => n !== null);
    layout.nodes = nodes;
  }
  const viewport = parseViewport(value["viewport"]);
  if (viewport !== null) layout.viewport = viewport;
  return layout;
}

/**
 * Parse an arbitrary `ui` value into a typed {@link UiLayout}, dropping anything
 * malformed. Returns `undefined` when there is no usable layout to keep.
 */
export function parseUi(value: unknown): UiLayout | undefined {
  if (!isRecord(value)) return undefined;
  const xyflow = parseXyflow(value["xyflow"]);
  if (xyflow === null) return undefined;
  return { xyflow };
}
