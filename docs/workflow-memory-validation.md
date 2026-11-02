# Workflow memory qualification

Recorded 2026-11-02. These are isolated automated results, not evidence of
retention in the user's live Hindsight service.

| Check | Result |
| --- | --- |
| Python 3.11 full workflow suite, with optional environment dependencies | 285 tests, pass; 1 expected skip |
| Python 3.14 full workflow suite, `-S` standard library path | 285 tests, pass; 4 expected skips |
| Frontend typecheck, shared web/Desktop tests, both production builds | Pass; 37 test files, 352 tests |
| Asset verification and dashboard entry loading | 8 asset hashes and bundle size verified; 4 entry tests pass |
| Workflow Python lint and diff whitespace | Pass |
| Isolated Hermes canonical test runner | 133 pass, 2 expected skips across 5 files |
| Isolated Hermes `scripts/check` | All 14 source/architecture checks pass |
| Prepared Hindsight provider suite with its locked test dependencies | 153 tests pass, including 12 new record-contract tests |
| Actual prepared host/provider class bridge | 6 cases pass: manual/scheduled context calls and headless calls, under in-process and hosted isolation |
| Exported host/provider source patches | Both apply cleanly to the pinned baselines; all 6 provider files match the tested candidate |

The bridge exercises the normal workflow settings, snapshot and outbox service,
not only a raw retain call. Each run completes and emits four queued effects.
The provider's injected SDK verifies the configured primary and existing mirror
destinations. There are zero real service requests, model calls or production
configuration changes. Every bridge profile is temporary and removed on exit.

The workflow suites cover dormant upgrades, legacy policy preservation,
transaction rollback and fail-open capture, synthetic secret redaction, owner
binding, loops/repeated review attempts, automatic retry archives, retry/resume
revision fencing, competing dispatchers, cancellation, process-loss recovery,
late acknowledgements and queued/unknown reconciliation without resending.
A second fake provider exercises the same contract. Host tests exercise scope,
secret ownership, capability grants, schedule consent, stale destination/selection,
context-only opt-out and deadline quarantine. Provider tests cover normal retain
shaping, primary/mirror routing, synchronous versus asynchronous receipts,
partial-bank ambiguity and cancellation of pending sends.

The host's existing caller-profile diagnostic tests originally failed on the
untouched Windows baseline because Python repr escapes backslashes. The
contribution corrects those assertions rather than changing production scope
logic. Test environments and PM tool stores are separate from gateway execution.

## Deployment boundary

The generic host extension is on local branch `codex/workflow-memory-host` in
`.cache/hermes-memory-host`, based on
`50d2adf1cd070b178eb454d86c794f72933b9518`. The user's installed Hermes checkout
was verified still on `main` at that base, with its original seven modified
catalog/PM files. No host/provider source was applied there, and the gateway was
not restarted or updated. The source contributions are also preserved as
[portable patches](provider-patches/README.md), so they do not depend solely on
an ignored local worktree.

The implementation checkpoint is on `codex/workflow-memory-foundation`; this
qualification was recorded before committing that checkpoint. No code push,
merge or Beads remote sync is included in this qualification.

## Live acceptance boundary

The shared web/Desktop component and transport contracts pass automated checks.
The new memory panel has not been exercised in the user's running Desktop.
The current gateway lacks the prepared host capability, and the installed
Hindsight plugin lacks the record implementation. Installing either would cross
the user's explicit no-gateway-update boundary.

The remaining acceptance step needs separate authorization for deployment and
a named disposable profile/bank. It must verify actual retention/readback where
supported, keep asynchronous acceptance distinct from visibility, exercise the
physical web/Desktop UI, and restore only that test profile's settings and
fixtures. It must not use or clear the existing production bank. Hindsight's
prepared client-effect lookup is unsupported, so an async queued receipt alone
cannot close that live gate. This distinction is tracked in `hwp-6pu.11.5`;
neither that issue nor the parent milestone is complete.
