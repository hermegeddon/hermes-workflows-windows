# Prepared host and Hindsight record extensions

## Generic Hermes host

`hermes-memory-records.patch` exports the isolated local Hermes fork branch
`codex/workflow-memory-host` at
`C:/Users/jmdzi/workspace/hermes-workflows-windows/.cache/hermes-memory-host`.
Its base is `NousResearch/hermes-agent`
`50d2adf1cd070b178eb454d86c794f72933b9518`. The installed checkout and running
gateway remain on their existing code; the patch contains no vendor-specific
implementation or new model tool.

The reviewed local host checkpoint is
`2f9bd9d42e5a51d45df63b37d4cfdecc9f1d98b9` on that isolated branch. It has
not been merged into the installed checkout or pushed.

The additive provider defaults are inert. Consumers use the new public
`PluginContext` operations or `MemoryRecordClient`, with owner-scoped secrets,
separate manual/scheduled capability consent, frozen destination/context,
bounded calls and typed receipts. Core's existing hosted-provider bridge carries
the same JSON contract. Provider selection alone does not authorize writes.

The contribution includes behavior tests for the new public interface and
small existing-test corrections: consent-only capability assertions accept new
registry entries, and Windows subprocess diagnostics compare escaped paths
correctly. The latter failures were reproduced on the untouched baseline.
The required host source checks all pass, as do 133 tests across five relevant
test files (two expected skips). See the provider guide included in the patch
for the exact public contract and [qualification](../workflow-memory-validation.md)
for the complete automated/live distinction.

Review from an isolated checkout of the pinned Hermes base:

```powershell
git apply --check <path-to-hermes-memory-records.patch>
git apply <path-to-hermes-memory-records.patch>
```

This prepares source only. Applying it to the installed checkout, updating the
gateway, publishing branches and live bank testing are separate actions.

## Companion Hindsight writer

`hindsight-memory-records.patch` is a reviewable provider contribution for the
Hermes workflow-memory milestone. It is separate from the Python workflow engine
and from the generic host extension. It has not been applied to the installed
Hindsight plugin or a running gateway.

Baseline: `vectorize-io/hindsight` commit
`d952b6e655fccab4581ee0722b51c5f103873d12`, subtree
`hindsight-integrations/hermes`, integration version 1.2.3. The audited installed
`__init__.py` SHA-256 is
`1b1436b80e271be8915a743e696fd63e590ab0938ffff5fb581fb1882cf1a10a`.
Line endings in the patch are normalized to LF.

The provider implements the new optional public memory-record contract. The
workflow plugin and Hermes core never access its private bank/client internals.
It preserves existing provider routing and retain shaping. Context-only mode
opts out; async acceptance is queued, synchronous success is acknowledged, and
ambiguous/partial delivery is unknown. It advertises neither idempotency nor
client-effect receipt lookup. Unknown effects require explicit reconciliation.

The patch includes 12 new real-provider-class tests with an injected SDK and
small test fixture portability corrections: home directories are isolated on
Windows too, and the executable fixture uses the native suffix. The complete
provider suite passed on native Windows: 153 tests with the provider's own
locked development dependencies, without calling a real memory service.

Review in an isolated checkout of the pinned Hindsight repository:

```powershell
git apply --check <path-to-hindsight-memory-records.patch>
git apply <path-to-hindsight-memory-records.patch>
```

The generic Hermes host extension must also be available before consumers can
use the new capability. Enablement, live bank testing and deployment are
separate steps; this artifact does not change provider selection or credentials.
