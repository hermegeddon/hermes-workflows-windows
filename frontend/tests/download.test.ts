import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { downloadTextFile } from "../src/templates/download";

describe("downloadTextFile", () => {
  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => "blob:fake");
    URL.revokeObjectURL = vi.fn();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("builds an object URL, clicks an anchor carrying the filename, and revokes", () => {
    const clicked: string[] = [];
    const realCreate = document.createElement.bind(document);
    const spy = vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = realCreate(tag);
      if (tag === "a") {
        (el as HTMLAnchorElement).click = () => clicked.push((el as HTMLAnchorElement).download);
      }
      return el;
    });

    downloadTextFile("wf-1.workflow.yaml", "id: wf-1\n");

    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(clicked).toEqual(["wf-1.workflow.yaml"]);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fake");
    spy.mockRestore();
  });
});
