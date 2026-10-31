import { describe, it, expect } from "vitest";
import { buildSeedWorkflow } from "../src/templates/seed";
import type { Scope, Trigger } from "../src/api/types";
// The REAL canonical validator (pure core module), not a copy — proves the seed
// the modal posts will actually be accepted by the create route.
import { validateWorkflow } from "@hermes-workflows/core/validation/validateWorkflow.ts";

describe("buildSeedWorkflow", () => {
  it("seeds a single finish node and no edges", () => {
    const wf = buildSeedWorkflow({
      id: "abc",
      name: "ABC",
      scope: { type: "global" },
      trigger: { type: "manual" },
    });
    expect(wf.version).toBe(1);
    expect(wf.edges).toEqual([]);
    expect(wf.nodes).toHaveLength(1);
    expect(wf.nodes[0]).toMatchObject({ id: "finish", type: "finish" });
  });

  it("passes the canonical validator for a manual global workflow", () => {
    const wf = buildSeedWorkflow({
      id: "abc",
      name: "ABC",
      scope: { type: "global" },
      trigger: { type: "manual" },
    });
    expect(validateWorkflow(wf).valid).toBe(true);
  });

  it("passes the canonical validator for a cron project workflow", () => {
    const wf = buildSeedWorkflow({
      id: "nightly",
      name: "Nightly",
      scope: { type: "project", projects: ["proj-a"] },
      trigger: { type: "cron", schedule: "0 5 * * *" },
    });
    expect(validateWorkflow(wf).valid).toBe(true);
  });

  it("carries the chosen scope and trigger through unchanged", () => {
    const scope: Scope = { type: "projects", projects: ["a", "b"] };
    const trigger: Trigger = { type: "cron", schedule: "*/5 * * * *", timezone: "UTC" };
    const wf = buildSeedWorkflow({ id: "x", name: "X", scope, trigger });
    expect(wf.scope).toEqual(scope);
    expect(wf.trigger).toEqual(trigger);
  });
});
