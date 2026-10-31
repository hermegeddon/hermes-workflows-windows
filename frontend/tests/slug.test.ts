import { describe, it, expect } from "vitest";
import { isValidSlug } from "../src/templates/slug";

describe("isValidSlug", () => {
  it("accepts letters, digits, hyphen, and underscore", () => {
    expect(isValidSlug("deploy")).toBe(true);
    expect(isValidSlug("nightly-build_2")).toBe(true);
  });

  it("rejects spaces, punctuation, slashes, and the empty string", () => {
    expect(isValidSlug("")).toBe(false);
    expect(isValidSlug("bad id")).toBe(false);
    expect(isValidSlug("dots.allowed?")).toBe(false);
    expect(isValidSlug("../escape")).toBe(false);
  });
});
