# Windows startup and host latency investigation

Investigation date: 2026-11-02. Beads: `hwp-6pu.34`, confirmed process-launch
defect `hwp-6pu.34.1`. Source baseline: `bbf91f1`; measurements below include
the working-tree process-launch fix. The optional OpenSecondBrain adapter
(`hwp-6pu.42`) is deferred by user direction and excluded from this release.

## Confirmed defect and correction

`run_process()` previously called `subprocess.Popen`, assigned the Windows job
and resumed the suspended child synchronously on the workflow event loop.
If process creation stalls, that entire loop stalls: other runs, heartbeats,
and coroutine cancellation cannot advance. This is a concrete defect even when
the OS delay itself cannot be reproduced on demand.

A deterministic regression gates `Popen` with a threading event and asks an
independent coroutine to advance before releasing it. The original code fails
this test (3.256 seconds, heartbeat did not advance). The corrected code passes.
A separate regression uses a real `Runtime` and SQLite database and proves that
another workflow is dispatched while the first launch remains gated.

Process creation, job assignment and resume now run in an executor thread.
`ProcessLaunch` owns the process and temporary capture files independently of
the awaiting coroutine. The existing step deadline starts before launch, so
executor queueing and startup count against it. Cancellation or timeout marks
the launch abandoned; a late Windows child is never resumed and is reaped by
its owner. Cleanup of an already-created process uses its own thread so a
saturated default executor cannot prevent cancellation cleanup. Successful
completion still terminates the job/process group before reading capture files.

Six regressions cover loop responsiveness, peer workflow dispatch, late-child
cancellation and timeout, saturated-executor cleanup, and Windows job setup
failure. Existing process-tree and output tests remain in the full suite.
No production deadline was increased.

## Measurements

These are bounded local observations, not latency guarantees. Both interpreters
ran natively on Windows. Optional dependencies were available to Python 3.11;
Python 3.14 used `-S` for the standard-library-only suite.

| Check | Python 3.11.11 | Python 3.14.9 |
| --- | --- | --- |
| Full suite | 296 tests, OK, 1 expected skip; 262.381 s | 296 tests, OK, 4 expected skips; 250.212 s |
| Loaded adoption readiness, 8 repetitions | 0.812–1.031 s; all pass | 0.848–1.100 s; all pass |
| Loaded isolated agent-worker readiness, 8 repetitions | 0.985–1.890 s; all pass | 0.914–1.052 s; all pass |
| Synthetic process diagnostic | 16/16 successful | 16/16 successful |
| Maximum observed heartbeat gap, 30 ms requested interval | 52.456 ms | 45.401 ms |

The loaded tests use a writer against the same fixture database: 5 ms inside a
transaction, then 10 ms idle. Neither interpreter recorded writer errors.
Forced timeout diagnostics were also verified. These repetitions did **not**
reproduce the historical 6/8-second readiness failures.

The synthetic diagnostic ran each case four times. Observed wall-time ranges:

| Case | Python 3.11 | Python 3.14 |
| --- | --- | --- |
| Python first code | 117.626–147.914 ms | 85.756–233.998 ms |
| Native cmd | 77.208–121.715 ms | 47.294–122.089 ms |
| Native PowerShell | 326.364–465.582 ms | 296.920–418.212 ms |
| Synthetic agent worker | 330.099–501.510 ms | 239.538–362.882 ms |

The worker uses deliberately fake host/agent modules in a temporary directory.
It exercises request loading, worker initialization, progress and response
serialization without a provider or model. It does not measure real model
startup. Child timestamps distinguish first executed Python/PowerShell code
from the parent startup span; cmd has no child timestamp. `launch_ms` includes
executor queueing, capture setup, process creation, job setup, resume and return
to the loop. It is not a kernel-only measurement. Python 3.11's coarse Windows
monotonic clock can report zero for a short launch span. Heartbeat measurements
include timer resolution and scheduler delays; an early 10 ms probe was
discarded in favor of the reported 30 ms measurements.

### Existing gateway, read-only

Four probes each for `default` and `workflow-qa` used the current installed
gateway, authenticated WebSocket client and `plugins.list` RPC. All eight
succeeded without creating sessions, running workflows, reloading plugins or
changing configuration. Results across the eight probes:

| Stage | Observed range |
| --- | --- |
| Control status | 2.869–51.116 ms |
| Profile-specific endpoint discovery | 8.825–75.772 ms |
| WebSocket connection | 5.577–111.142 ms |
| `plugins.list` RPC | 9.566–17.427 ms |

The first sandbox-only probe could not access the private gateway endpoints.
Repeating the same read-only check in the host context succeeded. Sandbox
access failure is not evidence of a slow gateway. These measurements cover
control/discovery/WebSocket/plugin-list paths; they do not constitute an ASGI
route or native Desktop workflow acceptance test for the modified backend.

## Remaining uncertainty and separate host behavior

The historical native cmd duration of 8390 ms and hosted PowerShell durations
over 22 seconds did not separate queueing, process creation, first child code
and execution. The corrected diagnostic makes that separation possible, but
does not establish why the OS delayed those particular executions.

Likewise, historical 10-second WebSocket and 30/60-second Desktop/plugin-host
timeouts have not been reproduced in this run. A healthy current connection
does not prove those intermittent observations fixed, nor prove a common cause
with the synchronous launch defect.

There is a separate verified stale-router mechanism in the inspected Hermes
host baseline (`50d2adf1cd`): `plugin_host_child.op_asgi` caches apps by API path
in `asgi_apps`, while `op_unload` removes plugin references/modules without
invalidating that cache. This explains the previously observed old exporter
after supported plugin reload. A user restart resolved that earlier acceptance
case. It does not explain every timeout.

An isolated host correction is now prepared as
[`host-patches/plugin-asgi-reload.patch`](host-patches/plugin-asgi-reload.patch),
on branch `codex/plugin-asgi-reload` in `.cache/hermes-latency-host`, based on
the same `50d2adf1cd` checkout. It has not been applied to installed Hermes or
the running gateway. It evicts cached apps under the unloaded plugin's resolved
directory, so dashboard IDs can differ from backend manifest keys and unrelated
cached apps stay warm. Cache generation checks also prevent an import already
in progress from restoring a stale app after unload. Plugin code executes
outside the cache lock; an in-flight old request may finish, but cannot poison
subsequent requests.

The ordinary reload test fails on the untouched baseline, with both normal and
failing unload callbacks. A separate gated cold-import test reproduces the race
in the initial eviction-only correction and passes with the generation guard.
Final native Windows host validation through `scripts/run_tests.sh`: 52 passed,
5 expected skips, across reload, host, route admission, secondary-profile and
consent modules on Python 3.14. All 14 `scripts/check` guards pass. The same
matrix exposed a separate baseline Windows fixture mismatch: a generated JS
asset used native CRLF while its exact-byte assertion expected LF. The patch
includes an explicit LF fixture writer; production serving and assertions stay
unchanged. Its baseline failure is retained separately.

The patch applies cleanly to both the untouched Hermes baseline and the isolated
memory-host extension worktree. This is source compatibility, not proof of a
combined deployment. The installed checkout still has its original seven dirty
files; this investigation did not change them. Host preparation is `.34.2`, and
the fixture correction is `.34.3`.

The parent `.34` retains the unreproduced historical delays for recurrence
capture and release assessment. Release `.16` must disclose unresolved
host-specific observations. Completed process ownership work is tracked
separately in `.34.1`; Beads remains the source of follow-up status.

## Post-restart live qualification

After the user restarted the gateway, the updated persistent backend passed
qualification in the existing `workflow-qa` profile (`.34.4`). This restart
did not deploy the isolated host patch or the memory-host extension.

Eight fresh read-only control/discovery/WebSocket/plugin-list probes succeeded
across `default` and `workflow-qa`. WebSocket connection times were
5.863–654.674 ms and plugin-list RPCs were 8.834–67.743 ms. These remain bounded
observations, not proof that the historical intermittent delays are fixed.

Five uniquely owned workflows used prefix `latency-live-qa-c6863d1b`:

| Check | Live result |
| --- | --- |
| Native cmd then PowerShell | Completed, followed by a waiting human review. cmd duration 65 ms, launch 23 ms; PowerShell duration 359 ms, launch 27 ms. New `launch_ms` telemetry confirms the changed persistent executor ran. |
| Web review | Approved in the Codex in-session browser; review and finish nodes visibly completed. |
| Desktop review | Approved in the installed native Desktop app under `workflow-qa`; review and finish nodes visibly completed. |
| Three-second step timeout | Workflow failed as expected with `TimeoutError: step exceeded 3 seconds`. |
| Cancellation and peer progress | The worker-free peer completed in 333.492 ms while the owned PowerShell child remained active. Cancellation through the web UI settled the run as cancelled and the exact PID/start-time-owned child was reaped. |

The long-running script was started through the backend qualification helper;
UI interaction only inspected results, approved worker-free reviews and
cancelled the owned test run. No workflow model or memory calls were made.
The in-session browser retained its authenticated session. An initial Edge
attempt needed login and was closed in favor of the user's requested in-session
browser. Desktop was launched from its existing installed build.

Guarded cleanup disabled only these five ownership-verified fixtures and kept
their run history. The temporary QA script setting was restored. Byte hashes
of default/QA config and environment files matched the original snapshot;
QA database settings, all pre-existing QA run/attempt rows and the complete
default workflow history also matched. No provider/bank destination changed.

Live evidence is retained under ignored `.cache/release-checks/latency-live/`:
`ready.json`, `original.json`, `run-states.json`, `audit.json`, `restored.json`,
`web-completed.png`, `desktop-completed.png` and `web-cancelled.png`.
Fresh transport evidence is `latency-gateway-post-restart.json` and its log.

### Publication CI exposed another fixture timing limit

CI `41459085866` on the published process fix `caa114f` passed five jobs and
the Windows 3.11 standard-library suite. The optional Windows 3.11 suite failed
only `test_parallel_fan_in_prompt_mapping_and_params` at its existing eight-second
wait. Its diagnostics show the prompt and all three agent steps completed,
with the final finish node pending at the snapshot; the trace subsequently
records completion 9.736 seconds after run creation. That runner also measured
native PowerShell at 24062 ms. This is further timing evidence, not proof that
process creation and SQLite delays share a cause.

A controlled 650 ms delay per `Database.mutate_run` reproduced the old failure
locally: 13 mutations, all fan-in work completed, final settlement outside the
eight-second fixture budget. The corrected test uses an event barrier to prove
both parallel steps enter before either returns, replacing fixed 80/180 ms
sleeps. Only that test's final-result wait gets a bounded 30-second allowance;
the shared eight-second readiness helper and production deadlines stay intact.
All original fan-in, prompt, parameter, mapping and token assertions remain.

With the same controlled slowdown, the corrected test passes in 8.578 seconds
on Python 3.11 and 8.721 seconds on Python 3.14. A negative control forcing
serial execution is rejected on both runtimes. The complete workflow test
module passes all 26 tests on each runtime (one optional skip on 3.14).
Beads `.34.6` tracks the test-only correction and fresh full-suite/hosted
qualification; this does not close the broader historical-delay investigation.
Evidence: `fan-in-before.log`, `fan-in-after311.log`, `fan-in-after314.log`,
`fan-in-serial311.log`, `fan-in-serial314.log`, `fan-in-module311.log` and
`fan-in-module314.log` under `.cache/release-checks/`.

### Loop completion and independent CI budgets

The subsequent optional Windows 3.14 run exposed a separate six-second wait in
`test_reaching_finish_cancels_an_unrelated_active_branch` (`.34.7`). A controlled
950 ms delay on each of seven SQLite mutations reproduces it locally: the short
branch completes and the long branch remains active while the finish operation
is still being persisted. The old test fails after its six-second allowance;
the probe ends in 7.047 seconds.

The corrected fixture uses an asyncio event to require the long branch to enter
before the short branch succeeds. The long branch waits for cancellation rather
than sleeping for an arbitrary duration. Its bounded 30-second completion wait
also requires the driver lease to be released, which happens after cancelled
branches have drained. Assertions check a successful short branch, completed
finish step, observed cancellation, persisted cancelled attempt, and absence of
the worker's deliberately returned late output. The shared six-second helper
and production deadlines are unchanged.

The same slow-write probe passes in 7.078 seconds on Python 3.11 and 7.162 seconds
on Python 3.14. A negative control corrupting the finish operation's cancellation
state fails on both runtimes. All 17 loop tests pass on each. Evidence is in
`loop-finish-before.log`, `loop-finish-after311.log`, `loop-finish-after314.log`,
`loop-finish-negative311.log`, `loop-finish-negative314.log` and the corresponding
`loop-finish-module*.log` files under `.cache/release-checks/`.

The Windows 3.11 CI cancellation was a separate job-budget problem (`.34.8`):
its passing standard-library suite took 631.706 seconds, consuming most of the
15-minute allowance shared with another full optional-adapter suite. CI now
uses independent standard-library and adapter jobs for every OS/Python pair.
Each retains its own 15-minute cap and runs a complete suite. Adapter jobs still
lint, every Python job verifies assets, and both frontend jobs remain required.
The resulting matrix contains eight Python jobs plus two frontend jobs. There
were no branch-protection check contexts or repository rulesets to migrate when
the Python job names changed.

## Repeating the synthetic diagnostic

From the repository root, using the desired Python interpreter:

```powershell
python scripts/diagnose_runtime_latency.py --iterations 4 --max-seconds 60 --out latency.json
python -m unittest discover -s tests -p test_process_lifecycle.py -v
```

The diagnostic uses only the standard library and this plugin, and emits no
tokens, prompts from user sessions or provider responses. It does not contact
the gateway, memory service or models. The iteration/time budget limits
measurement scheduling and step awaits; it is **not** a hard wall-clock kill
for an uninterruptible OS process-creation call. Its owning thread must still
finish and clean up a late child, and interpreter shutdown can wait for that
thread. Preserve the JSON and environment information when investigating a
recurrence rather than raising production deadlines.

Local evidence retained under ignored `.cache/release-checks/`: full suite logs
`latency-full311.log` and `latency-full314.log`; negative control
`process-loop-before.log`; corrected synthetic reports `latency-idle311.json`
and `latency-idle314.json`; loaded reports `startup-load-latency-v2-311.json`
and `startup-load-latency-v2-314.json`; sanitized transport report
`latency-gateway.json` and host-context log `latency-gateway-host.log`.
Host evidence: `asgi-reload-baseline.log`, `asgi-reload-race-before.log`,
`asgi-asset-fixture-baseline.log`, `asgi-reload-final-tests.log` and
`asgi-reload-final-check.log`.
