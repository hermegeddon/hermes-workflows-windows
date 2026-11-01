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
| Default project agent execution | Implemented native card creation/polling, profile/model/skills/runtime/retry options, workflow metadata and durable handles. Global, unbound and `board:false` nodes remain direct; unrepresentable overrides fail explicitly | `kanban.py`, `Engine`, compile-preview routing and native/isolated tests; hwp-6pu.6 |
| Board/project resolution and task links | Explicit run project wins over declared first project; missing boards auto-ensure and archived boards fail. Existing gateway dispatcher owns claims/caps. Adoption respects internal dependency order and preserves native parent gates | `board_for`, scoped native connections, `adoption.py`; hwp-6pu.6, hwp-6pu.7 |
| Adopted cards | Implemented literal/typed task references, structured-ID extraction, concurrent/sequential scopes, native review, restart receipts and bounded blocked/stuck handling. Current-host dependency gates intentionally prevent pinned force-promote behavior for umbrella children or cyclic/external unfinished parents. Windows web/Desktop live adoption and cancellation passed | U3/U4, `test_adoption.py`, frozen reference fixtures and native verification; hwp-6pu.7 |
| Managed scratch/worktree workspaces and stacked branches | Native board cards support scratch, dir and pre-materialized linked worktrees. Adopted stacks persist branch context and replayable fast-forward barriers; dirty/mismatched/conflicting/locked trees fail without destructive repair. Windows web/Desktop acceptance passed with real native workers and commits | U4, `test_worktrees.py`, `verify_native_worktrees.py`; hwp-6pu.8 |
| Prompt nodes and ancestor input references | Implemented for DAGs/cycles with runtime channel-availability checks and single-pass named mapping substitution; unavailable current-iteration values fail rather than becoming empty text | `specs.resolve`, `Engine._execute`, loop input fixtures; hwp-6pu.5 |
| Human review through API, tool, CLI, web and Desktop | Implemented with allowed decisions, notes and immutable run-spec review options; maintained web/Desktop clients fence requests with attempt IDs and handle fast re-entry/late acknowledgements | Review loop/restart/stale-token tests. Re-entry clears old decisions instead of retaining the pinned upstream value; hwp-6pu.5 |
| Chat reply review routing | Missing: no captured origin or uniquely-waiting-gate `pre_gateway_dispatch` hook | U6; hwp-6pu.10 |
| Worker-free PR wait | Partial: full GitHub PR URL, open/merged/closed handling, token and timeout work; numeric/contextual PR lookup is absent | `Executor._wait`; U1 and upstream `wait.py`; hwp-6pu.15 |
| Single-flight, cancel and retry | Implemented with leases and explicit node/full retry; external native work and upstream `resume` default-selection semantics remain partial | `db.py`, `Engine.retry`, `cli.py`; U3; hwp-6pu.4, hwp-6pu.15 |
| Cron and schedule actions | Functional Python scheduler and SQLite schedules; not native Hermes Cron registration/tagging | `schedules.py`, `runtime.py`, schedule/restart tests; U5; hwp-6pu.14 |
| Event trigger declarations | Declared and validated. Automatic webhook/GitHub/API-event-to-run wiring is absent in both pinned upstream and this backend | U1 explicitly defers host event firing. Preserve declarations in hwp-6pu.5; actual event firing is an extension, not missing pinned parity |
| Kanban lifecycle advance | Python supervisor polls owned cards and native final run summaries, resumes the same attempt on restart, and reconciles terminal/superseded card cleanup. Blocked cards wait up to the persisted node deadline; lifecycle hooks are not required for progress | U5; `KanbanExecutor`, native/isolated tests; hwp-6pu.6. No POSIX detached commands |
| Result delivery and lifecycle notices | Missing native router, run origin/default target, `[SILENT]`, per-card subscription defaults and node overrides | U6; hwp-6pu.9 |
| OpenSecondBrain | Partial presence/config badge only; no actual status probe, write policy, event writes or retrospectives | `api.py` `/o2b-status`; U7; hwp-6pu.11 |
| Telemetry and dangerous-command approvals | Partial final token/duration aggregates and worker-stage diagnostics; native live observers and approval-request UI integration are absent | `agent_worker.py`, `executor.py`; U8; hwp-6pu.12 |
| Traces and log export | Partial SQLite lifecycle events included on export when enabled; not upstream redacted JSONL event/marker/attempt coverage | `api.py`, `db.py`; U8; hwp-6pu.12 |
| Portable template export | Partial cached snapshot plus generic adaptation guide; structured de-binding, placeholder inventory and composite provenance/model cache are missing | `api.py` export-template; U9; hwp-6pu.13 |
| Settings | Implemented subset in SQLite; upstream config/env/default precedence and Kanban/memory/tick settings are missing | `settings.py`; U10; hwp-6pu.14 |
| Compile preview, API and command compatibility | Dashboard routes are implemented; plan content includes board routing and adoption fields. Upstream CLI/tool verbs such as `resume` differ | `specs.compile_preview`, `api.py`, `cli.py`, `__init__.py`; U10; hwp-6pu.5/.6/.7/.13/.14/.15 |
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
Hermes dashboard using the existing auth configuration.

Native Hermes Desktop subsequently passed the same loop in the local default
scope using its installed plugin bundle and SDK transport. The run was started
from Desktop's workflow actions; Request changes returned to a usable review
with an empty note, and approving that review completed the run. Read-only
inspection confirmed two distinct review attempt identities, iteration 1, and
separate persisted notes for needs_changes and approved. No provider calls or
shell execution were involved. This completes the local Desktop loop gate before
Kanban work; remote and remaining feature checks still belong to the final matrix.

The Kanban checkpoint adds 16 tests covering direct/board routing, options,
restart, loop/retry identity, real subprocess death after card commit, ambiguous
creation acknowledgements, atomic metadata publication, cancellation during an
uncommitted create, timeout cleanup and profile/path/ownership fences. The full
96-test suite passed on Windows Python 3.11, Windows Python 3.14 without optional
packages (two skips), and macOS Python 3.14 without optional packages (six skips).
`scripts/verify_native_kanban.py` also passed against the installed native APIs
on Windows and macOS in temporary homes. The real native dispatcher claimed cards
under a concurrency cap and native completion APIs persisted final summaries;
an injected spawn callback launched zero model workers.

Post-restart Windows acceptance then exercised two real native workers on the
disposable `workflow-qa-live` board, assigned to the dedicated `workflow-qa`
profile. A scripted localhost provider supplied deterministic tool/model replies;
no paid model calls were made. The web dashboard started the run and requested
changes after the first native result. Loop re-entry created exactly one new card
and attempt. The second worker committed context compression, then delivered the
same exact result marker through native `task_runs.summary`. Desktop approved
the fresh review and both frontends showed completion. Read-only checks confirmed
two cards, two worker attempts, two native runs and two review decisions, with
distinct handles and preserved notes. This host compressed in place; a live
session-ID rotation on an older Hermes build was not claimed. One Desktop
approval request encountered ECONNRESET; retry succeeded without duplicating the
decision. The QA profile's original config and environment were restored exactly,
the local provider was stopped, and the disposable workflow was disabled while
retaining cards/history. Screenshots and the acceptance report are in the ignored
`.cache/dashboard-check/kanban-live-qa/` directory.

This check also found that an implicitly selected project was missing from the
run-list project column. New runs now retain that binding in `project_id`, covered
by explicit and implicit project regression assertions. Existing run snapshots
are preserved. After a gateway restart, native Desktop started a project-scoped
finish-only workflow and displayed `workflow-qa-live` in the completed run's
Project column. Read-only checks confirmed the persisted project and board;
the two prior cards and native worker runs were unchanged. The fixture was then
disabled. Evidence is in `.cache/dashboard-check/project-label-check.json` and
`project-label-desktop.png`; no worker or model calls were needed.

Final review added a regression for a failed native worker whose summary declares
success: summary tokens classify only completed workers, while explicit native
metadata retains upstream precedence. The resulting 97-test suite passed on
Windows Python 3.11 and 3.14 (two optional-dependency skips on 3.14). The installed
native check now also restarts the supervisor before completion, verifies the
same cards and attempt identities, then cancels a separate queued run through
the native archive API without touching the completed cards. It passed with both
Windows runtimes and launched zero model workers. Its polling phases now have
independent deadlines and report the last state on timeout. Native plugin
registration, unload, dashboard mounting, review and loop-review HTTP checks also
passed with the Hermes-managed runtime. Lint, all eight asset hashes and the four
dashboard-loader tests passed; frontend source and bundles were unchanged.

The adoption checkpoint (2026-11-01) adds durable scope plans and immutable
attempt handles. Board-local receipts are committed atomically with native audit
events and assignment/promotion/review updates, while the workflow write fence
serializes cancellation and retry. Native assignment/promotion helpers commit
independently and reject nested transactions, so the adapter composes their
guarded column changes under the native write transaction instead of publishing
half a handoff. It requires the native audit and profile-normalization APIs;
workers, concurrency and claim ownership remain native.

Frozen extraction and ordering decisions were generated directly from the pinned
Python engine, with source SHA-256 recorded in
`tests/fixtures/adoption-reference.json`. Regenerate them with
`python scripts/generate_adoption_reference.py`. Isolated regressions include
process death after the review commit followed by an offline reviewer result,
sequential recovery, scope errors, profile fences, cancellation and blocked-clock
recovery. `scripts/verify_native_adoption.py` exercises real installed native
implementer/reviewer claims, final summaries and dependency ordering without
launching model workers. User card IDs, bodies, creators and dependency links are
preserved. A receipt alone does not count as review completion: a subsequent
native run is required.

Installed Hermes' promotion API no longer accepts `force`, and both claim paths
re-check unfinished parents. The adapter therefore fails clearly on an excluded
umbrella parent still gating its children, an unfinished external parent, or a
cyclic scope. It never removes links or bypasses the native claim rule. This is
an explicit difference from the pinned engine's forced-promotion fallback.
Live web/Desktop adoption acceptance passed on November 1, as detailed below.
This checkpoint does not claim managed worktree/stack support or full feature parity.

Final Windows validation passed 119 tests on Python 3.11 and Python 3.14 without
optional packages (two skips). This includes 20 adoption tests and two new WAL
startup tests. Concurrent initialization exposed a pre-existing `journal_mode`
lock race: WAL switching now retries only SQLite BUSY/LOCKED within a 15-second
budget and restores the connection's busy timeout; permanent errors propagate.
Twelve additional concurrent-migration rounds (48 child initializations) passed
on managed Python 3.14. The worker-stage diagnostic test now allows three seconds
for cold Windows startup before timing out its intentionally stalled loader.
Native adoption verification passed with both Windows runtimes, and existing
native card creation, restart, cancellation and concurrency checks still passed.
No model workers were launched. Lint, asset verification and all four loader
tests passed; frontend source and bundles were unchanged.

Live adoption validation exposed an installation-import bug that isolated engine
tests did not cover: the directory entrypoint and dashboard router imported the
process-global `hermes_workflows` package after changing `sys.path`. An older
copy in another profile could therefore supply the gateway's engine even when
the selected plugin directory was current. Directory registration now uses
Hermes' profile namespace; the router resolves the backend belonging to its own
real installation path, with an isolated fallback for file-only hosts. Four
new regressions cover canonical-module poisoning, distinct copies/concurrent
loads, failed-import rollback and matching dashboard routing. The native verifier
now uses Hermes' actual directory loader with a deliberately poisoned canonical
package. Registration, unload and review/loop HTTP contracts passed, as did the
full 123-test suites on Windows Python 3.11 and 3.14 (three optional skips on
3.14). A gateway restart is required to replace already-loaded legacy modules.

The first live fixture was aborted and is not acceptance evidence. Native
auto-decomposition processed its triage cards before explicit adoption, assigning
cards to configured profiles. One verification-engineer session used the existing
ChatGPT subscription (24,326 input / 222 output tokens); no claim of zero external
usage is made for that attempt. All five disposable cards were accounted for;
nonterminal fixture cards were archived, native runs reclaimed, and the QA
profile's config/environment restored exactly. Audit artifacts are in the ignored
`.cache/dashboard-check/adoption-live-qa/` directory. Replacement fixtures must
publish atomically as sticky-blocked work or unassigned manual review, never as
globally visible triage/ready work, and a no-model attempt-identity canary must
pass before any new worker test.

The replacement live test passed on Windows after refreshing an additional stale
supervisor inside a long-running subscription proxy. Restarting the gateway alone
had not retired that process. Subsequent no-model canaries received current
attempt identities. Workflow `adoption-live-qa-v2-696a6406c06e` started in the web
dashboard, adopted a completed selector's structured task IDs, reordered the two
selected cards by their existing dependency, and ran each implementer and native
reviewer in sequence. Desktop approved the final human gate; both frontends
displayed completion. Desktop also started and cancelled
`adoption-cancel-live-qa-v2-4d7d270c104a`, leaving its adopted card unassigned in
manual review, with no native worker or card replacement.

The QA profile initially lacked the native review lane's required `sdlc-review`
skill. Two reviewer attempts failed before model admission; temporarily supplying
the bundled skill and using native unblock recovered the same workflow attempt.
The second implementer's startup also completed pending Hermes dependency setup.
The four successful native turns made eight requests to a guarded loopback fake
provider; session billing metadata confirms that endpoint. The two failed turns
recorded zero tokens. This local-only result applies to the replacement fixture,
not the aborted subscription-backed test above. Audit assertions verified the
original card IDs, titles, bodies, creators and dependency link, six durable
adoption/review receipts, and an untouched unrelated blocked sentinel. The QA
config and environment were restored byte-for-byte, the temporary skill and
backup copies removed, and both fixture workflows disabled. History and proof
images remain in `.cache/dashboard-check/adoption-live-qa-v2/`.

The managed-worktree checkpoint adds deterministic card branches and linked
checkouts through Hermes' hardened noninteractive Git runner. An OS repository
lock serializes workflow Git operations and is released on process death. A
stack records its repository, common Git directory, checked-out shared branch
and expected commit; each card's before/after commit barrier is persisted before
fast-forwarding that branch. Restart reconciles an already-applied barrier.
An explicit shared branch must already be checked out, an intentional difference
from the pinned implementation's automatic checkout. The backend never resets
files, removes worktrees, deletes branches or pushes commits.

All 137 tests passed on Windows Python 3.11 and managed Python 3.14 without
optional packages (three skips). Fourteen worktree regressions cover physical
commit ancestry, native review commits, interrupted barriers including actual
process death, occupied paths, dirty/conflicting/mismatched trees, completed-card
retry/inclusion, and a real Windows sharing lock followed by recovery. The
installed-native verifier passed on both runtimes, exercising dispatcher claims,
worker CWD/environment construction, restart and preservation of a dirty owned
worktree during native cancellation cleanup. Actual directory-loader, HTTP
review/loop contracts, lint, asset verification and four loader tests passed.

Live Windows acceptance then passed for `worktrees-live-qa-dd047395c8ca`.
After refreshing the gateway, dashboards and subscription proxy, a no-model
canary confirmed the loaded stacking implementation. The web editor started a
disposable project workflow; two native Hermes workers ran terminal commands in
separate linked checkouts under `Project żółć with spaces`. The first committed
`ed67085`; the second verified the first file and committed `9fdd7be` with that
first commit as its parent. Both durable barriers completed and the shared branch
`feat/workflow-live` reached the second commit. Desktop approved the human gate,
and both frontends displayed completion. The two scoped sessions used the guarded
loopback provider (five requests, zero guard rejections); no external model
provider was used for this fixture. QA configuration and environment were
restored byte-for-byte, the provider stopped, sensitive backup copies removed
and the fixture workflow disabled. Git checkouts and history remain intact;
audit and proof images are in `.cache/dashboard-check/worktrees-live-qa/`.
Remote worktree acceptance remains part of the final parity gate.

The final parity gate **hwp-6pu.16** requires reference fixtures for every partial
or missing contract, upgrade/clean-install tests and Windows plus remote owner
checks through both frontends. Ordinary CI uses temporary storage, stub native
adapters and mocked providers. Actual channel messages, vault writes and paid
provider calls require explicit live-test authorization and disposable targets.
Publishing a release is a separate explicit action after the gate passes.
