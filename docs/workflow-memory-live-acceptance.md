# Isolated live memory acceptance

This procedure follows the automated
[qualification](workflow-memory-validation.md). It preserves the user's explicit
no-gateway-update instruction. Preparing files or a local commit does not grant
permission to write to a memory service.

## Proposed target

A source-only inspection of the current profile found `mode: local_external`
and the existing local endpoint `http://127.0.0.1:9696`. No credential value or
production bank identifier was printed, and no service request was made during
preparation. This is a local service, not permission to reuse its current bank.
Its ingestion pipeline may use its own configured models and incur costs.

The session's concrete proposal is saved outside Git at
`.cache/memory-live-qa/proposal.json`:

| Property | Proposed value |
| --- | --- |
| New bank | `hwp-memory-qa-20261102-b5465437` |
| Profile home | `.cache/memory-live-qa/hwp-memory-qa-20261102-b5465437/profile` |
| Host | Isolated `codex/workflow-memory-host` checkout, never the installed checkout |
| Provider | Prepared Hindsight contribution in the disposable profile only |
| Record budget | At most eight small synthetic lifecycle records across two runs |
| Mirrors/additional banks | Disabled; no production bank is a destination |
| Gateway | No update, restart or configuration change |

The exact manifest identity must be reviewed again before execution. If the
bank already exists, stop without writing; do not reuse or delete it. The marker
is synthetic and contains no user/workflow data. No credential, provider config
or production profile is copied by the preparation step.

## Authorized execution procedure

1. Obtain explicit approval for the target above and its possible service-side
   ingestion costs. Any credential access needed later must be limited to this
   local service; never copy an entire profile or print secret-bearing config.
2. Use the prepared host interpreter and source in a separate process. Create
   only the proposed disposable home, keeping the normal profile registry and
   installed gateway/provider files unchanged. Record source/patch hashes and
   the bank's initial absence before creating it through a supported service API.
3. Configure Hindsight for the explicit endpoint and exact new static bank:
   no template, trusted repository routing, mirror, additional write banks,
   automatic conversation capture, or embedded daemon startup. Use a temporary
   workspace outside any trusted repository. Freeze the host-resolved context.
   Managed/scoped `HINDSIGHT_BANK_ID` can outrank `config.json`; verify the actual
   provider-owned routing and guard the SDK destination on every retain. Abort
   if the resolved endpoint/bank differs from the manifest. A destination digest
   alone cannot prove the human-readable bank name.
4. Through the new public host interface, run one worker-free completion and one
   review/re-entry workflow, with summaries enabled and node events disabled.
   Freeze per-run ownership and persist their effects before dispatch. Keep the
   eight-record budget across the entire acceptance attempt, including failures.
   Do not create an AIAgent, call a chat model, or send chat notifications.
5. Record each effect ID and its actual receipt. Read back the exact marked
   document/record using a supported non-generative service read API or the bank
   UI. A queued receipt, bank health probe or recall answer alone is insufficient.
   Bound polling; on timeout preserve `unknown`/`queued` and stop. Never resend
   an ambiguous effect to force a green result. Test a read-only reconcile and
   confirm the record count does not increase.
6. Exercise the memory settings/status in an isolated web instance. Verify the
   normal default profile remains unchanged and the new receipts are shown
   honestly. Actual Desktop integration with the new host remains a separate
   deployment decision if the existing Desktop cannot select that isolated
   backend; do not patch the running gateway to bypass this boundary.
7. Stop only the owned test process. Record exact profile files, bank identity,
   marker and effect/readback evidence. Disable the disposable workflow policy
   and verify no pending dispatch remains. Retain failed/ambiguous evidence.
   Delete a bank only with explicit cleanup authorization, exact name/marker
   verification and confirmation that it was created by this test. Never clear a
   production bank, matching prefix, or an unrecognized pre-existing bank.

The deployment-free service probe and web test can qualify part of `.11.5`.
That issue stays open until its physical Desktop and remaining scope acceptance
requirements are satisfied or explicitly recorded as unsupported. The
subsequent [execution report](workflow-memory-live-results.md) records the actual
authorized service, web, native Desktop and cleanup results separately from this
pre-execution procedure.
