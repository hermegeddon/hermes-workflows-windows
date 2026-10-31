// Development-only: execute the pinned pure TypeScript decision function with
// Node's type stripping. Neither Python tests nor the backend invoke Node/Bun.
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(fileURLToPath(new URL('..', import.meta.url)));
const upstream = resolve(root, '.upstream');
const file = resolve(root, 'tests/fixtures/loop-reference.json');
const fixture = JSON.parse(readFileSync(file, 'utf8'));
const commit = execFileSync('git', ['-C', upstream, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
if (commit !== fixture.reference.commit) throw new Error('Upstream checkout differs from the fixture pin');
execFileSync('git', ['-C', upstream, 'diff', '--exit-code', commit, '--', 'packages/core/src']);
const source = resolve(upstream, 'packages/core/src/runtime/advance.ts');
const { advance, selectOutgoing } = await import(pathToFileURL(source).href);
const canonical = execFileSync('git', ['-C', upstream, 'show', `${commit}:packages/core/src/runtime/advance.ts`]);
fixture.reference.source_sha256 = createHash('sha256').update(canonical).digest('hex');
for (const entry of fixture.cases) {
  const workflow = fixture.workflows[entry.workflow];
  entry.expected = advance(workflow, structuredClone(entry.run));
  if (entry.source) entry.selected = selectOutgoing(workflow, entry.run, entry.source).map(edge => edge.to);
  else delete entry.selected;
}
writeFileSync(file, JSON.stringify(fixture, null, 2) + '\n');
process.stdout.write(`Recorded ${fixture.cases.length} decisions from upstream ${commit.slice(0, 12)}\n`);
