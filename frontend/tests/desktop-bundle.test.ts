import { it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createHash } from "node:crypto";
import ts from "typescript";

it("ships a self-contained Desktop entry within the host limit with verified hashes", () => {
  const root = resolve(import.meta.dirname, "../../hermes-workflows-py/desktop");
  const bytes = readFileSync(resolve(root, "plugin.js"));
  const source = bytes.toString("utf8");
  expect(bytes.length).toBeLessThanOrEqual(512 * 1024);
  expect(source).not.toMatch(/\bimport\s*\(|process\.env|__HWP_STYLES_EMBEDDED_AT_BUILD__/);
  const ast = ts.createSourceFile("plugin.js", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const imports = ast.statements.filter(ts.isImportDeclaration).map((node) => (node.moduleSpecifier as ts.StringLiteral).text);
  expect(imports.length).toBeGreaterThan(0);
  expect(imports.every((name) => ["react", "react/jsx-runtime", "@hermes/plugin-sdk"].includes(name!))).toBe(true);
  expect(source).toContain(".hwp-desktop .react-flow");
  const manifest = JSON.parse(readFileSync(resolve(root, "assets.json"), "utf8"));
  expect(JSON.stringify(manifest)).toContain(createHash("sha256").update(bytes).digest("hex"));
});
