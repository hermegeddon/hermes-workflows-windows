import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, mkdtempSync, rmSync } from "node:fs";
import { dirname, resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import * as hostReact from "react";
import { render } from "@testing-library/react";

const here = dirname(fileURLToPath(import.meta.url));
const appRoot = resolve(here, "..");
const distDir = mkdtempSync(join(tmpdir(), "hermes-workflow-bundle-"));
const distFile = resolve(distDir, "index.js");

// The T0 acceptance criterion: the editor source builds to a single
// self-executing file that, when loaded against mocked host globals, calls
// register("workflows", â€¦) and mounts an @xyflow/react canvas on the host's
// React instance â€” proving the bundle shape before any real UI is built.
describe("T0 bundling spike â€” built plugin bundle", () => {
  let bundleSource = "";

  beforeAll(() => {
    execFileSync(process.execPath, [resolve(appRoot, "node_modules/vite/bin/vite.js"), "build", "--outDir", distDir, "--emptyOutDir"], { cwd: appRoot, stdio: "inherit" });
    bundleSource = readFileSync(distFile, "utf8");
  }, 180_000);
  afterAll(() => rmSync(distDir, { recursive: true, force: true }));

  it("emits a single self-executing file with no dynamic chunks", () => {
    expect(existsSync(distFile)).toBe(true);
    expect(bundleSource.length).toBeGreaterThan(1000);
    // IIFE/UMD single file â€” no code-splitting / dynamic import chunks.
    expect(bundleSource).not.toMatch(/\bimport\s*\(/);
  });

  it("contains no `process` reference (would ReferenceError in the browser)", () => {
    // The host loads this file directly; `process` is undefined there. react /
    // react-dom's NODE_ENV checks must be inlined at build time. jsdom defines
    // `process`, so this text guard â€” not the eval below â€” is what catches it.
    expect(bundleSource).not.toMatch(/process\.env/);
    expect(bundleSource).not.toMatch(/[^.\w]process[^.\w]/);
  });

  it("emits the xyflow stylesheet alongside the script", () => {
    expect(existsSync(resolve(distDir, "index.css"))).toBe(true);
  });

  it("registers \"workflows\" and mounts the app shell using the host React", () => {
    let reactAccessed = false;
    const registered: Record<string, unknown> = {};

    const sdk: Record<string, unknown> = {
      hooks: {},
      api: {},
      fetchJSON: async () => ({}),
      components: {},
    };
    Object.defineProperty(sdk, "React", {
      configurable: true,
      get() {
        reactAccessed = true;
        return hostReact;
      },
    });

    const win = globalThis as unknown as {
      window: Record<string, unknown>;
      __HERMES_PLUGIN_SDK__?: unknown;
      __HERMES_PLUGINS__?: unknown;
    };
    win.window.__HERMES_PLUGIN_SDK__ = sdk;
    win.window.__HERMES_PLUGINS__ = {
      register(name: string, component: unknown) {
        registered[name] = component;
      },
      registerSlot() {},
    };

    // Evaluate the IIFE bundle in this realm (jsdom window).
    new Function(bundleSource)();

    expect(reactAccessed).toBe(true);
    const App = registered["hermes-workflows"];
    expect(typeof App).toBe("function");

    const { container } = render(
      hostReact.createElement(App as React.ComponentType),
    );
    // The shell mounts using the host React (the SDK React getter fired above)
    // and renders its chrome â€” here the Open Second Brain (O2B) indicator.
    expect(container.textContent).toContain("O2B");
  });
});
