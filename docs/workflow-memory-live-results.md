# Native memory live acceptance

Passed on native Windows, 2026-11-02, after separate user authorization for the
named disposable bank and possible service-side ingestion costs. This supplements
the [automated qualification](workflow-memory-validation.md).

## Source and target

The workflow checkpoint is `121fb8f6532dc10421c315e2030066a39af32816` on
`codex/workflow-memory-foundation`. The host extension is
`2f9bd9d42e5a51d45df63b37d4cfdecc9f1d98b9` on the isolated
`codex/workflow-memory-host` branch. Hindsight uses the prepared pinned provider
contribution, copied only into a workspace-local QA profile.

The existing local service at `http://127.0.0.1:9696` was used with a new bank,
`hwp-memory-qa-20261102-b5465437`. A bank-list query established its initial
absence. Its config carried the unique synthetic marker from the proposal.
Provider routing and every SDK retain were guarded against any other endpoint
or bank, with mirrors and additional banks disabled. No production provider
configuration, source or gateway was updated.

## Results

| Check | Evidence |
| --- | --- |
| Worker-free completion | Run completed; terminal summary and retrospective captured |
| Worker-free review loop | Needs-changes re-entry, fresh review, approval and completion; terminal summary and retrospective captured |
| Actual writes | Four distinct effects and four durable pre-send reservations, within the authorized maximum of eight |
| Retention | Four documents read back from the exact new bank; every original JSON payload matched its local immutable effect |
| Extraction | Each read-back document had one memory unit |
| Receipts | Provider asynchronous receipts remained `queued`; separate readback evidence did not fabricate lookup capability or upgrade the ledger |
| Reconciliation and UI refresh | No additional retains or reservations |
| Web dashboard | Actual Hermes web shell and workflow settings showed Hindsight, disabled defaults, queued delivery, writes supported, lookup/idempotency unavailable |
| Native Desktop | Actual installed Hermes Desktop showed the same status through its remote-gateway transport to the isolated local backend |

The Desktop test used a separately approved temporary connection named
`Memory QA b5465437`, with only the disposable server's short-lived session
token. The original connection was not replaced or made secondary. After the
check, Desktop was restored to **This device / Backend ready / Capabilities**;
the temporary connection was removed, the original **This device** and
**mithraeum** connections remained, the sidebar layout was restored, and the
owned read-only web helper was stopped. Its local token file was removed.
The conversational model setup warning on the empty QA profile did not prevent
workflow-plugin operation; no conversational model was configured or invoked.

The first QA-only loop fixture lacked a distinct entry node and was rejected
before creating that second run. The corrected fixture was validated offline
and continued only the previously unstarted review workflow. The first two
accepted effects were not resent. A legacy bank-profile GET endpoint returned
410; ownership verification uses the supported bank-config `reflect_mission`
marker instead. These were probe corrections, not production workflow fixes.

## Cleanup and preserved evidence

After separate explicit cleanup authorization, the bank's exact name, unique
marker and all four document IDs were verified before deletion. A subsequent
bank-list query confirmed absence. No existing bank was mutated. Native writes
were disabled in the disposable profile, with no prepared/in-flight memory
effects or active test workers left running. The test profile and evidence stay
under ignored workspace storage, outside the normal profile registry.

Evidence directory:
`.cache/memory-live-qa/hwp-memory-qa-20261102-b5465437/` contains
`acceptance.json`, `write-reservations.json`, `ui-acceptance.json`,
`web-settings.png`, and `desktop-settings.png`. Logs and the reviewed proposal
are in its parent directory. No credential value is included in the reports.

The final full workflow suites passed 288 tests on each Python runtime (3.11:
one expected skip; 3.14 standard-library path: four expected skips). Subsequent
focused checks passed on both runtimes: 19 lifecycle/outbox tests, including
native Kanban worker completion with a foreign worker scope and output
minimization, and four live-probe safety tests. The safety suite imports no SDK
and calls no network service. Frontend source/builds remain the previously
qualified 352-test checkpoint.

The repeatable live runner is `scripts/check_memory_live.py`; its required
`--allow-live` switch is not a substitute for operator authorization. It refuses
an existing profile by default, reserves unknown sends against the write budget,
and supports only the narrowly checked unstarted-review continuation used here.
A fresh execution requires a newly authorized target; the deleted bank must not
be silently recreated.

This qualifies the native-provider memory milestone in isolation. It does not
deploy the host/provider changes to the normal gateway, certify an optional O2B
adapter, prove exactly-once delivery, or close the broader host-latency and final
cross-host release gates.
