# Hermes Workflows Python

This plugin runs workflow graphs on Python 3.11+ on Windows 11. Its engine, SQLite
storage, scheduler, CLI, and HTTP sidecar use the standard library. There is no Bun
or Node.js runtime dependency. Original React dashboard assets are preserved from
upstream commit `352e5ffb751e4798e66dcdf1dbe9a80dff768c0f`; maintained web and native
Desktop adapters reuse that editor with the Python backend.

## Install into Hermes

Use the Python interpreter from your existing Hermes environment. Copying the
plugin does not require a package installation or administrator privileges:

```powershell
# From the repository root. Replace this with your Hermes Python executable.
$Python = "C:\path\to\hermes\.venv\Scripts\python.exe"
& $Python .\hermes-workflows-py\install.py
```

The installer copies the plugin into `Path.home() / '.hermes' / 'plugins' /
'hermes-workflows'`. It refuses to overwrite an existing installation unless you
pass `--update`. `--destination` selects a different plugin directory. Stop the
running plugin before updating it, then restart Hermes and enable the
`hermes-workflows` plugin through its plugin manager.

For development, run commands from the `hermes-workflows-py` directory:

```powershell
Set-Location .\hermes-workflows-py
& $Python -m hermes_workflows --help
& $Python -m hermes_workflows serve --port 9123
```

The server binds to `127.0.0.1`. Keep it running for scheduled execution and
dashboard-triggered work. `--token VALUE` optionally requires `Authorization:
Bearer VALUE` on HTTP requests. The CLI also supports an editable backend install
with `pip install -e .`, but copying the complete plugin directory is the dashboard
installation method.

## Connect the existing dashboard

Hermes loads `register(ctx)` through the root `__init__.py`. This registers
`workflow_list`, `workflow_run`, `workflow_status`, `workflow_explain`, and
`workflow_review`, plus the `/workflow` slash command. Registration starts the
workflow supervisor immediately, reconciles schedules, and recovers expired run
leases without waiting for the first tool call. Agent/provider imports remain
lazy. Hosts with `ctx.on_unload` stop the supervisor and its workers on plugin
unload; process exit also performs cleanup. Runtimes and registered handlers are
bound to their Hermes home, including hosts serving multiple profiles.

The dashboard manifest references `dashboard/plugin_api.py`. That
file exports a FastAPI `router` for hosts supporting standard plugin route
handlers. FastAPI is supplied by the dashboard host; importing the core backend
does not require it. The installed Hermes version used for native verification
supports enabled user-plugin API routes directly, including a junction
installation. Its discovery and router mounting passed workflow creation,
polling, review, completion, filtering, and export tests.

The upstream React JS/CSS in `dashboard/dist/` stays unchanged. The active
frontend in `dashboard/compat/` is built from the maintained source in the
repository's `frontend/` directory and uses DOM dialogs for schedule editing,
deletion, workflow duplication, and retrying a node. These work in embedded
browsers without native `prompt`/`confirm` support. A small companion
entry adds Approve, Reject, and Request changes controls with an optional note
for waiting human-review steps in the run inspector. The backend enforces each
gate's allowed decisions. Dashboard extensions are separate from Hermes Desktop
extensions; the native Desktop entry described below uses its own SDK. After
changing the companion files or manifest, use Plugins → Rescan dashboard
extensions and reload the page; a gateway restart is unnecessary.

An embedded FastAPI/ASGI host also starts the supervisor at application startup.
Shared ownership prevents HTTP shutdown from stopping a supervisor still owned
by the gateway plugin.

Hermes versions that disable Python route loading for third-party plugins need
the HTTP sidecar. Configure the dashboard's authenticated reverse proxy to route
`/api/plugins/hermes-workflows/*` to `http://127.0.0.1:9123`, **preserving the
path and query string**. The sidecar also accepts unprefixed API paths. For an
existing ASGI host, mount `hermes_workflows.api:app` at the same API prefix.

The bundle expects the Hermes host globals `__HERMES_PLUGIN_SDK__` and
`__HERMES_PLUGINS__`; it is a host plugin, not a standalone HTML page. Model and
skill pickers continue to use the host's `/api/model/options` and `/api/skills`.
The sidecar serves copied assets under `/dashboard/` for inspection, while Hermes
owns mounting the dashboard tab.

## Enable Workflows in Hermes Desktop

The complete package includes `desktop/plugin.js`, a native Desktop adapter built
from the maintained editor. Keep this directory alongside `plugin.yaml` and
`dashboard/` when copying or linking the plugin. The copy installer includes it.
No additional Bun, Node, HTTP sidecar, or browser login is needed at runtime.

1. Install and enable the Python backend in the gateway profile as above.
2. In Hermes Desktop, open **Capabilities → Plugins**, search for **workflows**,
   and select the local **Windows-native Python DAG workflows** package.
3. Click **Rescan** if its Desktop half says `copying…` or is absent, then turn
   on its **Desktop** switch. The backend and Desktop switches are separate.
4. Open **Workflows** in the sidebar, or **Open Workflows** in the command palette.

Desktop materializes the linked package's entry into its app-level
`desktop-plugins` directory. Keep the package junction; do not add a second
Desktop junction. Rescan refreshes the copy after rebuilding. Desktop supports
live disable/re-enable without restarting the gateway.

The native adapter calls `ctx.rest` and the Desktop SDK's model/skills catalogs.
The selected gateway and profile determine the backend; that profile must have
the Python plugin enabled. Switching either resets the view and rejects stale
requests. A disconnected gateway shows a waiting state. Navigation stays inside
the Workflows view without overwriting Desktop's router. Styles are removed on
disable. Live verification covered Windows local/default, a separate Windows
`workflow-qa` profile, and a registered SSH gateway running macOS/default.
Identical workflow IDs in the QA profile and remote gateway kept independent
specifications and run histories through gateway/profile switches, cancellation,
retry, and review approval. SDK fixtures also cover stale requests and late
responses during scope changes.

For a free smoke test, open an existing manual human-review → finish workflow,
click **Play**, cancel the waiting run, retry it, and approve its review. The run
should finish as `completed`. No model calls are needed for these node types.

Frontend development requires Node only on the development machine:

```powershell
npm.cmd ci --prefix frontend
npm.cmd run typecheck --prefix frontend
npm.cmd test --prefix frontend
npm.cmd run build --prefix frontend
```

Run these from the repository root. The build emits both web and native bundles,
embeds the Desktop CSS into a single ESM file below the host's 512 KiB limit, and
updates integrity manifests (`dashboard/upstream.json`, `desktop/assets.json`).

## Run a Windows smoke workflow

In a second PowerShell session, with the sidecar running:

```powershell
Set-Location C:\path\to\hermes-workflows-py
$Python = "C:\path\to\hermes\.venv\Scripts\python.exe"
$Api = "http://127.0.0.1:9123/api/plugins/hermes-workflows"

# Script execution defaults to disabled, matching upstream.
Invoke-RestMethod "$Api/settings" -Method Put -ContentType application/json `
  -Body '{"scripts_enabled":true,"trace_enabled":true}'

& $Python -m hermes_workflows import .\examples\windows-smoke.workflow.json
$Run = Invoke-RestMethod "$Api/workflows/windows-smoke/run" -Method Post `
  -ContentType application/json -Body '{}'
Invoke-RestMethod "$Api/runs/$($Run.run_id)"

# After the run reaches waiting/review:
$Review = @{node_id="review"; decision="approved"; note="Verified on Windows"} | ConvertTo-Json
Invoke-RestMethod "$Api/runs/$($Run.run_id)/review" -Method Post `
  -ContentType application/json -Body $Review
```

CLI commands include `list`, `import`, `validate`, `run`, `status`, `review`,
`cancel`, `retry`, and `export`. A CLI `run`, `review`, or `retry` remains alive
until completion or the next review gate, so work survives the HTTP request that
started it. A CLI run without a sidecar can execute normally; schedules need a
persistent host or sidecar.

```powershell
& $Python -m hermes_workflows run windows-smoke
& $Python -m hermes_workflows status RUN_ID
& $Python -m hermes_workflows review RUN_ID review approved --note "Looks good"
& $Python -m hermes_workflows retry RUN_ID --node-id hello
```

## Storage and execution

Paths use `pathlib.Path`. The default database is `Path.home() / '.hermes' /
'workflows' / 'runs.db'`; `HERMES_HOME` can point at an isolated Hermes home.
Workflow files live in `workflows/global` and `workflows/templates`. JSON runs
without third-party packages; YAML uses the PyYAML already installed in Hermes.
New files use `.workflow.json`. Saving an existing YAML file writes JSON syntax,
which is valid YAML 1.2; comments and formatting are not retained. Canvas `ui`
metadata is preserved.

SQLite uses WAL, a busy timeout, separate connections per operation, and immediate
write transactions. Namespaced `py_*` tables do not overwrite the upstream Bun
schema. Existing Bun run records are not imported. Run creation enforces one
active run per workflow; configurable limits bound active runs and concurrent
steps. Run leases prevent separate gateway and dashboard processes from driving
the same run simultaneously.

The engine uses `graphlib.TopologicalSorter` for forward dependencies and
concurrent asyncio tasks. A workflow has exactly one entry. DFS identifies
feedback edges so loops can re-enter an earlier step without waiting for that
feedback edge on the first pass. Forward joins retain the all-parent barrier:
all forward predecessors settle, and at least one incoming branch must be
selected. Unselected branches become `skipped`. Edges support `node_status`,
`review_status`, and fallback routing (a fallback is used when conditional
siblings exist and none match). An explicit `finish` settles the whole run and
cancels remaining active work. A work failure with no selected outgoing edge
fails the run. Existing acyclic workflows without a finish keep implicit completion.

### Conditional loops

Cycles validate with a warning, require a distinct entry and reachable finish,
and re-enter when a newer completion or review decision routes back to a settled
node. Each execution gets a fresh iteration/attempt identity. Prior outputs,
review decisions/notes and wait deadlines are cleared from the current node;
attempt history remains available in storage. Self-loops are supported too.

`max_loop_reentries` in Settings defaults to **100 total node re-entries per run**.
Exhausting it fails the run with an explicit error. An operator's explicit retry
resets this budget. It is separate from an agent's `max_iterations` turn limit.

For a free example, import [`examples/review-loop.workflow.json`](examples/review-loop.workflow.json),
enable script execution in Settings, and click Play. **Request changes** runs the
shell step again and presents a fresh review; **Approve** completes it and
**Reject** fails it. This workflow makes no model requests.

Both maintained dashboards submit the displayed review's `attempt_id`. A delayed
request for an older iteration returns 409; a fast loop cannot hide the next
gate through polling/acknowledgement races. Legacy API clients may omit the token
and continue to act on the current gate. The `workflow_review` tool accepts the
same token; the standalone CLI accepts `review ... --attempt-id TOKEN`.
Channel values must exist in the current
source node state: unresolved inputs fail explicitly. `input_mapping` substitutes
named `{{placeholders}}` once; inserted output is never re-interpreted as another
template. Legacy mappings without local placeholders keep their Inputs block.

Supported nodes are `agent_task`, `script`, `prompt`, `condition`, `human_review`,
`finish`, and `wait`. Typed parameters use upstream's `text`, `enum`, `int`, and
`bool` schema; values are required unless marked `optional` or given defaults.
References support `{{params.name}}`, `{{nodes.step.output}}`, JSON output paths,
and `{{nodes.review.review_note}}`. Prompt nodes and run input layer instructions
into downstream agent tasks. Input references must point at graph ancestors.

Agent tasks run the existing Python `AIAgent` in an isolated Python worker. The
worker uses Hermes' provider resolver and profile environment, honors node
model/provider overrides and skills, and writes its result as UTF-8 JSON. The
gateway's environment and working directory stay isolated from parallel profiles.
The same Hermes Python interpreter and host modules must be available to the
worker. A missing provider/profile produces a persisted node error.

Native JSON specs may also set `max_iterations`, `max_tokens`,
`enabled_toolsets`, and `disabled_toolsets` to bound an agent step. If omitted,
the worker honors the profile's turn limit. `load_external_secrets: false`
explicitly skips external secret-manager hydration inside that worker, while
still loading the profile's dotenv and managed environment; the default is true.
Workers never trigger Hermes' automatic dependency installer. Timeouts name the
last worker stage, and incomplete agent responses settle as failures.

Scripts use `cmd.exe` by default on Windows. Set `shell` to `powershell`, `pwsh`,
`cmd`, or `sh` explicitly. PowerShell commands use UTF-16 encoded arguments with
UTF-8 output. `workdir` accepts native Windows paths, spaces, and Unicode. Script
environment inheritance is restricted to `script_env_allowlist`, optionally narrowed
by each node's `env`; Windows system and temporary-directory variables are always
provided. The default allowlist is empty, so add `PATH` when commands need PATH
lookup. Use absolute executable paths otherwise.

Workers launch suspended, join a Windows Job Object, then resume. Cancellation,
timeout, and host exit terminate the complete process tree. Large output is
captured to temporary files to avoid pipe deadlocks and is capped when loaded
into run state. Automatic retries apply only to transient agent errors and the
configured retry count; scripts are not automatically replayed.

Human review decisions persist across restarts. An in-flight command interrupted
by a host restart is marked failed when its expired lease is reclaimed, requiring
an explicit retry. External wait nodes resume polling with their original timeout
deadline. Retrying uses the live workflow configuration and rejects structural
graph changes; a selected node retry also resets its descendants.

Schedules use a Python five-field cron evaluator and persist next/last fire times.
Overlapping runs are skipped; missed intervals coalesce to one run on restart.
UTC works with the standard library on Windows. Other IANA timezones need timezone
data in the Hermes environment. Schedule edits, pauses, and deletion survive
restart. These schedules are managed by this backend, independently of Hermes'
native cron job store.

## Dashboard API

All paths below may be prefixed with `/api/plugins/hermes-workflows`.

| Resource | Routes |
| --- | --- |
| Workflows | `GET/POST /workflows`, `GET/PUT/DELETE /workflows/{id}` |
| Workflow actions | `PUT /workflows/{id}/enabled`, `POST .../validate`, `POST .../compile-preview`, `POST .../run` |
| Workflow downloads | `GET /workflows/{id}/export`, `GET .../export-template` |
| Runs | `GET /runs?scope=active\|all&workflow_id=...`, `GET /runs/{id}` |
| Run actions | `POST /runs/{id}/review`, `POST .../cancel`, `POST .../retry`, `GET .../export` |
| Schedules | `GET /schedules`, `PUT/DELETE /schedules/{id}`, `POST .../pause`, `POST .../resume`, `POST .../run` |
| Settings/catalog | `GET/PUT /settings`, `GET /profiles`, `GET /o2b-status` |

List envelopes, workflow details (`workflow`, `ui`, `path`), run/node state names,
epoch-second timestamps, summaries, validation issues, compile plans, and download
envelopes match the pinned dashboard. Errors use `{"detail":"..."}` with 400,
404, 405, or 409 status codes. Workflow creation returns 200, matching upstream.
Starting a run returns immediately with `run_id` and `status`; poll the run for
progress. A review body contains `node_id`, `decision`, optional `note`, and an
optional `attempt_id` to fence the decision to the exact displayed review.
`retry` accepts optional `node_id`; omitting it resets the full run.

## Compatibility boundaries

The execution foundation now stores a run revision and separate node-attempt
history, including iteration/attempt identities and durable external handles.
Cancelling or retrying a run revokes its previous driver lease; obsolete worker
results cannot overwrite a newer attempt. Transient agent retries retain their
individual outcomes. The current dashboard continues to read the latest node
state in `run.nodes`.

Database schema 2 migrates schema 1 transactionally on startup, preserving run
and spec snapshots, logs, settings, schedules and cached exports. Stop older
gateway/CLI processes before upgrading, then restart with the updated plugin;
do not run different backend versions against the same profile database. A newer
unknown schema is rejected. Tests cover rollback and simultaneous process startup.

The external-effect journal persists intent before an integration acts. An
operation left in flight by a lost driver becomes `unknown` and requires
reconciliation, including when its receipt was lost. Native Kanban creation can
reconcile automatically: its stable attempt key is looked up under the board's
write transaction, including archived cards. Other external operations are not
automatically sent again. Delivery and memory writes remain unimplemented.

### Native Kanban execution

Agent steps in a bound `project`/`projects` scope create native cards on the
project's board (the run's `project_id`, otherwise the first `scope.projects`
entry). A missing board is created; archived boards are not restored implicitly.
Global workflows, unbound project scopes and nodes with `board: false` use the
direct runner. A missing native API fails the board step instead of silently
running it directly. Compile preview reflects the node's routing choice.

Hermes' existing gateway dispatcher claims and executes these cards, applying
its native concurrency limits. The plugin does not launch another dispatcher or
worker pool. Keep the gateway and its Kanban dispatcher running for project
workflows; a standalone workflow CLI/server does not replace that service.
The normal dispatcher interval can delay a newly queued card.

Cards carry the profile, model, skills, runtime cap, native retry setting and
workflow/step IDs. An explicit `workdir` uses a native `dir` workspace; otherwise
the native dispatcher manages a scratch workspace. Managed Git worktrees and
card adoption remain separate features. Node-specific toolset, token/iteration
and external-secret overrides are not representable by the installed native
card API and fail explicitly; use `board: false` for those overrides.

Each logical attempt has a durable card identity and handle. A gateway restart
resumes polling the same card and preserves its deadline; loop re-entry and
explicit retry create distinct cards. Completion reads native `task_runs.summary`
and outcome metadata, without assuming a stable worker session ID. Native
workers own their retry policy; the Python engine does not retry them again.
Cancellation, superseded attempts, timeouts and early workflow completion archive
only cards whose ownership matches the stored attempt. Pending cleanup is retried
by the existing workflow supervisor. Active cards survive ordinary runtime shutdown.

The maintained web/Desktop controls already expose the node's board toggle.
After installing this change, restart the gateway and refresh/reopen the workflow
view before testing. No production cards or model workers were created during
the automated integration checks.

Native adapter availability can be inspected without executing workflows:

```powershell
& $Python .\scripts\verify_native_host.py --capabilities
```

The probe uses a temporary profile context, does not change `HERMES_HOME`, and
reports API signatures without invoking operations. Both the installed Windows
host and `mithraeum` exposed the candidate Kanban, dispatcher, worktree, cron,
config, delivery and hook APIs during verification. A headless probe has no live
delivery router; API availability is not a claim that those integrations execute
in this backend yet.

The dashboard's current client routes are implemented, but the Python backend
does not reproduce every upstream integration. Bound project agent tasks use
native Kanban; global and explicitly off-board tasks execute directly. Card
adoption, stacked releases, and managed worktree nodes fail validation; use
explicit `workdir` paths for existing directories.
Conditional loop edges are implemented. Workflow result delivery and
OpenSecondBrain writes are not implemented. The O2B badge reports local
installation/configuration only. Event triggers remain declarative, as upstream;
an external system can start a run through the run route.

`wait_for.github_pr_merged` accepts a full GitHub PR URL, with `GH_TOKEN` or
`GITHUB_TOKEN` available for private repositories. Numeric-only PR references
need conversion to URLs. Template export produces a cached offline spec and
adaptation guide; it does not call an LLM to de-bind installation-specific values.
Settings are persisted in this backend's SQLite store, independently of Hermes'
`config.yaml`; the returned schema lists the implemented settings.

Two deliberate graph compatibility choices remain: forward joins wait for all
parents (the pinned upstream core activates on any incoming route), and re-entered
reviews clear the previous decision (the pinned core retains it). These preserve
existing join behavior and require an actual new approval. Reference decisions
are recorded in `tests/fixtures/loop-reference.json` with the upstream commit and
source hash. Contributors can regenerate them with
`node --experimental-strip-types scripts/generate_loop_reference.mjs` against the
pinned `.upstream` checkout. Runtime and Python tests do not invoke Node or Bun.

The native Windows suite covers the API, actual CMD and Windows PowerShell,
Unicode paths/output, process-tree timeouts, reviews and recovery, parallel joins,
cross-process leases, schedules, asset hashes, and isolated worker integration.
The FastAPI adapter is also tested with actual FastAPI when its host dependencies
are available. Additional native probes use the installed Hermes dependency
environment without invoking its update launcher:

```powershell
# From the repository root; use the installed Hermes Python interpreter.
& $Python .\scripts\verify_native_host.py
& $Python .\scripts\verify_native_host.py --agent-preflight
# Explicitly permits a bounded real provider request:
& $Python .\scripts\verify_native_host.py --live-agent
```

The default probe uses temporary workflow storage and the actual Hermes
PluginContext, ownership ledger, dashboard discovery, and API route mounting.
Preflight resolves the configured provider without requesting a model response.
The live probe uses the default profile with no tools, bounded turn/token/time
limits, and an expected response marker; only the marker and usage metadata are
reported on success. A real default-provider request has completed successfully.
Rendering and authenticated interactions in the user's running dashboard remain
a separate manual end-to-end check.
