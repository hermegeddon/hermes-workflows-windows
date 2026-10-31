import { defineConfig, transformWithEsbuild } from "vite";
import { resolve } from "node:path";
import postcss from "postcss";
import { PLUGIN_VERSION } from "./plugin-version";

const placeholder = "__HWP_STYLES_EMBEDDED_AT_BUILD__";
export default defineConfig({
  esbuild: { jsx: "automatic", jsxDev: false },
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    __PLUGIN_VERSION__: JSON.stringify(PLUGIN_VERSION),
    __PLUGIN_BUILD__: "2",
    __HWP_DESKTOP_CSS__: JSON.stringify(placeholder),
  },
  build: {
    outDir: resolve(import.meta.dirname, "../hermes-workflows-py/desktop"),
    emptyOutDir: false, minify: "esbuild", cssCodeSplit: false,
    lib: { entry: resolve(import.meta.dirname, "src/desktop/plugin.tsx"), formats: ["es"], fileName: () => "plugin.js" },
    rollupOptions: {
      external: ["@hermes/plugin-sdk", "react", "react/jsx-runtime"],
      output: { inlineDynamicImports: true },
    },
  },
  plugins: [{
    name: "embed-desktop-css", enforce: "post",
    generateBundle: { order: "post", async handler(_, bundle) {
      let css = "";
      for (const [name, asset] of Object.entries(bundle)) {
        if (asset.type === "asset" && name.endsWith(".css")) { css += String(asset.source); delete bundle[name]; }
      }
      const tree = postcss.parse(css);
      tree.walkRules((rule) => {
        if (rule.parent?.type === "atrule" && /keyframes$/i.test(rule.parent.name)) return;
        // The first-party classes are already prefixed. Scope xyflow's shared
        // class names without breaking the first-party dropdown portals.
        if (rule.selector.includes("react-flow")) rule.selectors = rule.selectors.map((selector) => `.hwp-desktop ${selector}`);
      });
      for (const chunk of Object.values(bundle)) {
        if (chunk.type !== "chunk") continue;
        if (!chunk.code.includes(JSON.stringify(placeholder))) throw new Error("Desktop CSS injection marker missing");
        chunk.code = chunk.code.replace(JSON.stringify(placeholder), JSON.stringify(tree.toString()));
        // Vite's ES-library build preserves whitespace for downstream bundlers.
        // Desktop loads this entry directly, so finish the single-file build.
        chunk.code = (await transformWithEsbuild(chunk.code, "plugin.js", {
          minify: true, format: "esm", target: "es2022", legalComments: "inline",
        })).code;
        if (Buffer.byteLength(chunk.code) > 512 * 1024) throw new Error("Desktop plugin exceeds the supported 512 KiB entry limit");
      }
    } },
  }],
});
