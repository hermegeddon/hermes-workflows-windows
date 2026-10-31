const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');
const { resolve } = require('node:path');

const source = readFileSync(resolve(__dirname, '../hermes-workflows-py/dashboard/entry.js'), 'utf8');

function boot() {
  const registrations = [];
  const scripts = [];
  const register = (name, component) => registrations.push({ name, component });
  const registry = { register };
  const entry = { src: 'http://localhost/dashboard-plugins/@example/hermes-workflows/entry.js', getAttribute: () => 'example' };
  const document = {
    currentScript: entry,
    createElement: () => ({ attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } }),
    body: { appendChild: (script) => scripts.push(script) },
  };
  const React = {
    createElement: (type, props, ...children) => ({ type, props, children }),
    useState: (initial) => [typeof initial === 'function' ? initial() : initial, () => {}],
    useEffect: () => {},
  };
  runInNewContext(source, { window: { __HERMES_PLUGIN_SDK__: { React }, __HERMES_PLUGINS__: registry }, document, URL });
  return { document, registry, register, registrations, scripts };
}

test('entry registers immediately and loads original assets in the same profile', () => {
  const app = boot();
  assert.equal(app.registrations[0].name, 'hermes-workflows');
  assert.equal(app.scripts[0].src, 'http://localhost/dashboard-plugins/@example/hermes-workflows/compat/index.js');
  assert.equal(app.scripts[0].attributes['data-hermes-profile'], 'example');
});

test('only this child bundle is captured; other plugin registrations pass through', () => {
  const app = boot();
  const other = () => {};
  app.registry.register('another-plugin', other);
  app.registry.register('hermes-workflows', other); // Another script/profile.
  assert.equal(app.registrations.length, 3);
  const original = () => {};
  app.document.currentScript = app.scripts[0];
  app.registry.register('hermes-workflows', original);
  app.scripts[0].onload();
  assert.equal(app.registry.register, app.register);
  const tree = app.registrations[0].component({});
  assert.equal(tree.children[1].type, original);
});

test('load failure restores the host registry and gives an actionable error', () => {
  const app = boot();
  app.scripts[0].onerror();
  assert.equal(app.registry.register, app.register);
  const tree = app.registrations[0].component({});
  assert.equal(tree.props.role, 'alert');
  assert.match(tree.children[0], /Could not load Workflows/);
});

test('a bundle that never registers reports the failure instead of loading forever', () => {
  const app = boot();
  app.scripts[0].onload();
  const tree = app.registrations[0].component({});
  assert.equal(tree.props.role, 'alert');
  assert.match(tree.children[0], /did not register/);
});
