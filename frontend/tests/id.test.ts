import { describe, it, expect } from "vitest";
import { generateWorkflowId } from "../src/templates/id";
import { isValidSlug } from "../src/templates/slug";

describe("generateWorkflowId", () => {
  it("returns a 6-char lowercase id that passes the slug check", () => {
    for (let i = 0; i < 50; i++) {
      const id = generateWorkflowId();
      expect(id).toMatch(/^[a-z]{6}$/);
      expect(isValidSlug(id)).toBe(true);
    }
  });

  it("is practically unique across many calls", () => {
    const ids = new Set(Array.from({ length: 300 }, () => generateWorkflowId()));
    expect(ids.size).toBeGreaterThan(290);
  });
});
