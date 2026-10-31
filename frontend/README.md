# Dashboard development

This is a maintained copy of the React dashboard source from
`itechmeat/hermes-workflows` commit `352e5ffb751e4798e66dcdf1dbe9a80dff768c0f`.
The original prebuilt JS/CSS remains in `hermes-workflows-py/dashboard/dist/`.
This variant replaces native browser prompts with accessible DOM dialogs, and
builds into `hermes-workflows-py/dashboard/compat/`. The web companion entry adds
human review controls. A native Hermes Desktop SDK adapter in `src/desktop/`
reuses the editor with memory navigation, scoped SDK transport, and native review
controls, and builds into `hermes-workflows-py/desktop/plugin.js`.

```powershell
npm ci --prefix frontend
npm run typecheck --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
```

Node is required only for frontend development. The installed plugin needs
neither Node nor Bun. `vendor/core/` contains only the eight upstream schema,
validation, parameter, and plan modules used for frontend types and tests. None
is installed or executed as a workflow backend.

The build updates asset hashes in `dashboard/upstream.json` and
`desktop/assets.json`. Refresh the browser after building; use Desktop's Plugins
Rescan to refresh its materialized entry. A gateway restart is unnecessary.

Changes from upstream include the ActionDialog component and its callers,
dialog regression tests, frontend output paths, version resolution from the
Python plugin, and corrections to the upstream bundle registration test.
Upstream MIT licensing is reproduced in the plugin's THIRD_PARTY_NOTICES.md.
