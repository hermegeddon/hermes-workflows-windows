const { readFileSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { createHash } = require('node:crypto');
const root = resolve(__dirname, '../../hermes-workflows-py/dashboard');
const file = resolve(root, 'upstream.json');
const metadata = JSON.parse(readFileSync(file, 'utf8'));
metadata.local_files = Object.fromEntries(
  ['manifest.json', 'entry.js', 'style.css', 'compat/index.js', 'compat/index.css'].map(name =>
    [name, createHash('sha256').update(readFileSync(resolve(root, name))).digest('hex')])
);
writeFileSync(file, JSON.stringify(metadata, null, 2) + '\n');
const desktop = resolve(root, '../desktop');
const { existsSync } = require('node:fs');
if (existsSync(resolve(desktop, 'plugin.js'))) {
  writeFileSync(resolve(desktop, 'assets.json'), JSON.stringify({ files: {
    'plugin.js': createHash('sha256').update(readFileSync(resolve(desktop, 'plugin.js'))).digest('hex')
  } }, null, 2) + '\n');
}
