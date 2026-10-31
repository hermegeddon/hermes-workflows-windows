# Pinned upstream compatibility assessment

Assessment date: 2026-10-31. Reference:
[`itechmeat/hermes-workflows@352e5ff`](https://github.com/itechmeat/hermes-workflows/tree/352e5ffb751e4798e66dcdf1dbe9a80dff768c0f).
The shipped upstream dashboard assets use this same pin. Local behavior was
reviewed at baseline `03e472d`, with CI added in `e1bce60`.

This is a source and test inventory, not a claim that every upstream edge case
already passes. "Implemented" identifies working local coverage; "partial"
identifies a narrower contract or different semantics. Implementation tasks,
acceptance criteria, dependencies and completion status are authoritative in
Beads under epic **hwp-6pu**. This document records compatibility evidence.

## Reference evidence

The upstream references below are relative to the pinned repository, which is
available locally as the ignored `.upstream/` checkout. They can also be opened
under the pinned GitHub tree above.

| Reference | Evidence examined |
| --- | --- |
| U1: `packages/core/src/schema/{workflow,nodes,graph,run}.ts`, `schema/load.ts`, `validation/validateWorkflow.ts` | Fields, graph rules, triggers, node states and validation |
| U2: `packages/core/src/runtime/{advance,conditions,runMutations}.ts` | Routing, re-entry by completion sequence, finish outcomes and state mutation |
| U3: `hermes_workflows/{engine,resume}.py`, `tests/python/test_cli_resume.py`, `test_engine_transient_retry.py` | Adopt sequencing, recovery, failure handling and resume |
| U4: `hermes_workflows/bridge/{boards,kanban,links,profiles,worktree}.py`, `executor/{kanban_executor,composite}.py` | Project boards, native cards, dispatcher integration, dependencies and workspaces |
| U5: `hermes_workflows/{hooks,runtime,plugin}.py`, `bridge/cron.py` | Card-driven advance, safety polling, native cron and plugin wiring |
| U6: `hermes_workflows/{notifications,notify_sender,origin_capture,gate_reply}.py`, `bridge/notify.py` | Delivery, subscriptions, origin and chat review routing |
| U7: `packages/core/src/memory/`, `tests/python/test_engine_memory.py` | Memory providers, redaction, policy and retrospectives |
| U8: `hermes_workflows/{observer,telemetry,trace,redact}.py`, `tests/python/test_engine_telemetry.py` | Live worker telemetry, approval state and JSONL traces |
| U9: `packages/core/src/templates/`, `hermes_workflows/template_export.py`, `tests/python/test_template_export.py` | Typed slots, deterministic de-binding, provenance and optional model hints |
| U10: `hermes_workflows/{config,cli,tools}.py`, `dashboard/plugin_api.py`, `tests/python/test_dashboard_*` | Settings, command/tool surfaces, API contracts and schedules |

Local implementation evidence is in
[`hermes_workflows/`](../hermes-workflows-py/hermes_workflows/),
[`tests/`](../tests/), and [`frontend/tests/`](../frontend/tests/).

## Capability inventory

| Capability | Current Python behavior | Coverage / remaining owner |
| --- | --- | --- |
| Spec import, CRUD, enable/disable and export | Implemented for JSON and optional host-provided YAML; preserves UI metadata and safe paths | `specs.py`, `api.py`; workflow/API and loader tests. Broader schema equivalence: hwp-6pu.5 |
| Typed parameters and run input | Implemented for text/enum/int/bool, defaults, validation and prompt layering | `fill_params`, `Engine.start/_execute`; exact cross-surface contract audit: hwp-6pu.13, hwp-6pu.15 |
| Acyclic execution, conditions and fallback | Implemented with asynchronous steps, handled failure/fallback, hard abort and whole-run finish settlement. Forward joins intentionally preserve the existing all-parent barrier instead of pinned upstream any-parent activation | `Engine._plan`, `selected_edges`, pinned decision fixtures and join regression; hwp-6pu.5 |
| Conditional loop edges | Implemented with DFS feedback-edge analysis, fresh iterations, self-loops, wait-clock resets and a persisted configurable re-entry budget | `graph.py`, `Engine._plan`, `test_loops.py`; hwp-6pu.5 |
| Iterations, attempts and external-effect recovery | Foundation implemented: transactional schema-2 migration, run revision and node-attempt identities/history, durable handles, unique driver leases, stale-result fences and explicit unknown-effect reconciliation. Loop traversal now uses these identities; native integration dispatch remains separate work | `migrations.py`, `db.py`, `effects.py`, `Engine.drive`, `test_foundation.py`; hwp-6pu.4 |
| Script nodes | Implemented with enable gate, environment allowlist, timeout and process-tree cancellation; native CMD/PowerShell/Unicode tests pass | `executor.py`; retain coverage during hwp-6pu.5 and hwp-6pu.16 |
| Direct agent nodes | Implemented with profile/model/skills, bounded isolated Python worker, final output and transient retries | `agent_worker.py`, `Executor`, agent-result fixtures; retain global and `board=false` execution during hwp-6pu.6 |
| Default project agent execution | Missing native Kanban path: current compile preview marks every agent `off_board: true` and execution is direct | U1/U4 default project nodes to native cards. hwp-6pu.6 |
| Board/project resolution and task links | Missing native board auto-ensure, dispatcher ownership and project dependency links | U4/U5; hwp-6pu.6, hwp-6pu.7 |
| Adopted cards | Missing: `adopt` is rejected; task-id extraction, card review, dependency ordering, sequential scopes and stuck/blocked handling are absent | U3/U4 and `test_engine_adopt.py`; hwp-6pu.7 |
| Managed scratch/worktree workspaces and stacked branches | Partial: explicit `workdir` works; host-managed workspace lifecycle is absent and worktree/stack is rejected | U4 and `test_dispatch_worktree_conformance.py`, `test_adopt_branch_anchor.py`; hwp-6pu.8 |
| Prompt nodes and ancestor input references | Implemented for DAGs/cycles with runtime channel-availability checks and single-pass named mapping substitution; unavailable current-iteration values fail rather than becoming empty text | `specs.resolve`, `Engine._execute`, loop input fixtures; hwp-6pu.5 |
| Human review through API, tool, CLI, web and Desktop | Implemented with allowed decisions, notes and immutable run-spec review options; maintained web/Desktop clients fence requests with attempt IDs and handle fast re-entry/late acknowledgements | Review loop/restart/stale-token tests. Re-entry clears old decisions instead of retaining the pinned upstream value; hwp-6pu.5 |
| Chat reply review routing | Missing: no captured origin or uniquely-waiting-gate `pre_gateway_dispatch` hook | U6; hwp-6pu.10 |
| Worker-free PR wait | Partial: full GitHub PR URL, open/merged/closed handling, token and timeout work; numeric/contextual PR lookup is absent | `Executor._wait`; U1 and upstream `wait.py`; hwp-6pu.15 |
| Single-flight, cancel and retry | Implemented with leases and explicit node/full retry; external native work and upstream `resume` default-selection semantics remain partial | `db.py`, `Engine.retry`, `cli.py`; U3; hwp-6pu.4, hwp-6pu.15 |
| Cron and schedule actions | Functional Python scheduler and SQLite schedules; not native Hermes Cron registration/tagging | `schedules.py`, `runtime.py`, schedule/restart tests; U5; hwp-6pu.14 |
| Event trigger declarations | Declared and validated. Automatic webhook/GitHub/API-event-to-run wiring is absent in both pinned upstream and this backend | U1 explicitly defers host event firing. Preserve declarations in hwp-6pu.5; actual event firing is an extension, not missing pinned parity |
| Kanban lifecycle advance | Missing native completed/blocked hooks and owned-card lookup; current direct workers advance through the Python runtime | U5; hwp-6pu.6. Keep a Python-native recovery path instead of copying POSIX detached commands |
| Result delivery and lifecycle notices | Missing native router, run origin/default target, `[SILENT]`, per-card subscription defaults and node overrides | U6; hwp-6pu.9 |
| OpenSecondBrain | Partial presence/config badge only; no actual status probe, write policy, event writes or retrospectives | `api.py` `/o2b-status`; U7; hwp-6pu.11 |
| Telemetry and dangerous-command approvals | Partial final token/duration aggregates and worker-stage diagnostics; native live observers and approval-request UI integration are absent | `agent_worker.py`, `executor.py`; U8; hwp-6pu.12 |
| Traces and log export | Partial SQLite lifecycle events included on export when enabled; not upstream redacted JSONL event/marker/attempt coverage | `api.py`, `db.py`; U8; hwp-6pu.12 |
| Portable template export | Partial cached snapshot plus generic adaptation guide; structured de-binding, placeholder inventory and composite provenance/model cache are missing | `api.py` export-template; U9; hwp-6pu.13 |
| Settings | Implemented subset in SQLite; upstream config/env/default precedence and Kanban/memory/tick settings are missing | `settings.py`; U10; hwp-6pu.14 |
| Compile preview, API and command compatibility | Dashboard routes are implemented; plan content reflects direct-only execution, and upstream CLI/tool verbs such as `resume` differ | `specs.compile_preview`, `api.py`, `cli.py`, `__init__.py`; U10; hwp-6pu.5/.6/.13/.14/.15 |
| Web and native Desktop integration | Implemented and live-tested on Windows default/named profiles and remote macOS review workflows | `frontend/src/desktop`, SDK owner/stale-request tests; each feature must include relevant UI coverage, final gate hwp-6pu.16 |

## Execution and integration decisions

Pure Python orchestration, SQLite WAL, pathlib paths and the existing frontend
contract remain constraints. The TypeScript/Bun CLI, Unix service launchers and
POSIX detached-process mechanics are implementation details to replace, not
requirements to reproduce. Native Hermes Kanban/Cron/delivery remain integration
targets; optional Git and O2B operations use bounded argument-vector subprocesses
only when those capabilities are installed.

The **hwp-6pu.4** foundation implements migrated execution identities and
host capability adapters. Its tests cover existing runs, crash windows, late
results, revision/iteration changes and stale completions. Loop (**hwp-6pu.5**)
and Kanban (**hwp-6pu.6**) implementations both depend on it;
adoption depends on Kanban, and stacking depends on adoption. Delivery shares the
durable effects foundation. Chat review also depends on loop iteration identity;
memory depends on settings; native telemetry depends on the Kanban adapter.

Do not promise exactly-once external delivery across a network acknowledgement
loss. Use stable effect keys, receiver idempotency where available and explicit
reconciliation/reporting for ambiguous results. Likewise, a database lease alone
cannot prove that an external card or Git operation did not already happen.

Native config and cron migration must designate one authority before enabling
the replacement. Preserve current values, existing schedules and standalone
mode; running both schedulers would risk duplicate triggers.

## Verification boundary

The current Windows baseline passed 38 Python tests, 337 frontend tests, four
loader tests, typechecking, Ruff and both production builds. The CI asset verifier
adds five corruption/path/size checks. These counts document this assessment's
baseline, not a permanent test-count requirement.

Hosted CI subsequently exposed a recovery/retry interleaving (hwp-6pu.18): an
old driver could continue with its old spec after recovery committed a failure
and a retry installed an updated prompt. The driver now exits on the terminal
recovery snapshot, with a deterministic interleaving regression. Broader attempt
and external-effect fencing remains in hwp-6pu.4. Hosted Windows also required
integration-test budgets above its measured 18-second PowerShell invocation;
production execution defaults were not increased.

Foundation verification adds migration/backfill/rollback and cross-process startup
fixtures, cancellation-ignoring worker and transient-retry histories, per-iteration
identity checks, real subprocess crash/effect reconciliation tests, and concurrent
async profile-scope tests. Read-only probes of the installed Windows and remote
macOS host found all candidate native API surfaces. These probes ran in temporary
profile homes and did not execute board, cron, delivery or provider operations.

The previous live macOS check exercised installation, scope isolation and review
cancel/retry/completion. It did not certify macOS agent execution, board dispatch,
worktrees, delivery or memory. Those require their own acceptance evidence.

Loop fixtures contain 21 decisions recorded by executing the pinned pure
TypeScript engine during development; Python tests consume only the saved JSON.
The re-entered-review case records the raw upstream result plus an explicit safer
Python expectation: the previous decision is cleared and the run waits again.
Additional integration tests exercise actual shell loops, repeated reviews,
restart, stale tokens, cancellation, wait clocks, budgets and per-iteration data.
All-parent forward joins and legacy acyclic implicit completion remain supported
extensions; these are not a claim of byte-for-byte decision equivalence.

The foundation/loop checkpoint passed 80 backend tests on Windows Python 3.11,
Windows Python 3.14 without optional packages (two skips), and macOS Python 3.14
without optional packages (six platform/dependency skips). Frontend checks passed
341 tests, typechecking and web/Desktop builds; four loader tests, Ruff and all
eight asset hashes passed. An isolated installed-Hermes host test also mounted
the dashboard routes and completed a repeated review while rejecting the old
attempt token. Those checks use temporary storage.

After the October 31 gateway restart, the real default-profile database reported
schema 2, retained all 15 existing runs, backfilled 24 historical attempts and
passed SQLite's integrity check. A new no-model loop completed through the
authenticated web dashboard: Request changes exposed a fresh review with a
cleared note, then Approve completed it. Persisted attempt history retained both
decisions with distinct identities; the API handler rejected the old token with
409 against that live run. Shell execution remained disabled. The previous
temporary dashboard listener had expired and was replaced by a loopback-only
Hermes dashboard using the existing auth configuration. Native Desktop loop
interaction remains part of the final acceptance matrix.

The final parity gate **hwp-6pu.16** requires reference fixtures for every partial
or missing contract, upgrade/clean-install tests and Windows plus remote owner
checks through both frontends. Ordinary CI uses temporary storage, stub native
adapters and mocked providers. Actual channel messages, vault writes and paid
provider calls require explicit live-test authorization and disposable targets.
Publishing a release is a separate explicit action after the gate passes.
