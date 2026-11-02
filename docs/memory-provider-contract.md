# Workflow memory contract decision

Audited 2026-11-01 for `hwp-6pu.11.1`. This is a source contract audit,
not a live Hindsight health or persistence check.

## Decision

Build provider-neutral, deterministic lifecycle records and a durable local
outbox first. Keep it dormant in ordinary plugin operation. The native adapter
requires a supported Hermes capability that does not exist in the audited host.
Do not substitute provider internals or fabricated conversation/built-in-memory
writes. Native adapter, settings activation and live acceptance remain blocked
until that capability is implemented and audited.

Implementation update (2026-11-02): the generic host extension now exists on the
isolated local fork branch `codex/workflow-memory-host`, with a separately
reviewable Hindsight contribution. Neither is deployed. The Python adapter and
opt-in settings use that public contract; see [workflow memory](workflow-memory.md)
for the implemented policy and the distinction between automated and live checks.

Hindsight is the first conformance target. Its public retain tool is a useful
provider-side implementation building block, but it is not a generic Hermes
record-write contract. A model/tool-free workflow summary is separate from the
normal memory behavior of agents that execute workflow nodes.

## Source baseline and reproduction

* Workflow plugin baseline: `3fa8202257463b85b264e94e7865798c8d38088c`.
* Pinned workflow upstream: `352e5ffb751e4798e66dcdf1dbe9a80dff768c0f`.
* Installed Hermes checkout's main ref: `50d2adf1cd070b178eb454d86c794f72933b9518`.
  The installed working files can include local changes; the hashes below,
  rather than the ref alone, identify what was inspected.
* Installed Hindsight integration: catalog pin
  `d952b6e655fccab4581ee0722b51c5f103873d12`, integration version 1.2.3.
* Public [Hermes MemoryProvider source](https://github.com/NousResearch/hermes-agent/blob/main/agent/memory_provider.py)
  was also checked on the audit date; its shared interface has no arbitrary-record writer.

Reproduce the source hashes and isolated probes with Python 3.11+:

```powershell
python scripts/audit_memory_contract.py <Hermes-source-directory> <Hindsight-plugin-directory>
```

The script reads Python source, executes the standard-library-only base class,
and extracts the public Hindsight dispatch function into fake handlers. It never
imports or initializes Hindsight, reads bank configuration/secrets, or calls a
network service. These probes do not establish that a real provider write works.

| Inspected source | SHA-256 |
| --- | --- |
| `agent/memory_provider.py` | `ac81dcf489c698891f0edebfe26845a75c50d2bde77ec3be8d7938a85c79f1fc` |
| `agent/memory_manager.py` | `88027d7ae6ee27b5abbd266619a8fbf37a561465acc1a30e0bb27954ea6f8657` |
| `plugins/memory/__init__.py` | `fa7d31ac9ae103ec9afad7cfa76ab3f88e00b19379ee83cde590c3b9dfb9535e` |
| `hermes_cli/plugins.py` | `0da0ebe88da76f4a73d77fcc6942ef549154abbc573d52d74c1608bcb61efce4` |
| `hermes_cli/plugins_capability_ctx.py` | `62cae94191ffebc47294031e3ee26184df9db823a1861bf84b407e84cd76ae83` |
| `hermes_cli/plugin_host.py` | `c1b64e49fcdedbb42a6567c20cb0118c131711dbe1953322d85d3b455bdaa66f` |
| `hermes_cli/plugin_host_child.py` | `98bec1588c1968919ff9d78d9cc2fb6f2cb997020bb3543df22c74ff1bbe9e0c` |
| Hindsight `__init__.py` | `1b1436b80e271be8915a743e696fd63e590ab0938ffff5fb581fb1882cf1a10a` |

## Supported-path matrix

| Requirement | Audited public surface | Finding |
| --- | --- | --- |
| Provider discovery | `plugins.memory.load_memory_provider(name, *, register_skills=None)` | Returns a fresh provider or hosted proxy; respects disabled plugins. Loading is not pure discovery. No workflow-specific writer capability is advertised. |
| Normal agent activation | `agent.agent_init_memory_provider.resolve_memory_provider(name, say=None)` | May attempt recovery/install of missing providers. Do not use as a read-only workflow probe. |
| Profile ownership | `MemoryManager.initialize_all(session_id, **kwargs)`; provider `initialize(session_id, **kwargs)` | Manager binds a profile and refuses mismatched/missing caller scope in a multiplexed host. Provider instances, secret scope and background contexts must remain profile-owned. |
| Provider selection | Hermes `memory.provider` | One external provider plus built-in memory. Selecting Hindsight does not opt in to extra workflow writes. |
| Arbitrary workflow records | `MemoryProvider` ABC | No `write_record`, `retain`, `write_event` or `write_retrospective` method. Default lifecycle hooks may be no-ops. |
| Provider tools | `MemoryManager.get_all_tool_schemas()` and `handle_tool_call(tool_name, args, **kwargs)` | Scoped, redacted dispatch is available to a manager owner. Tool names/arguments and results are provider-specific; schema text cannot safely determine which tool means retain. |
| Built-in mirror | `on_memory_write(action, target, content, metadata=None)` | Means a successful actual MEMORY.md/USER.md tool write. Not a general event hook. |
| Delegation | `on_delegation(task, result, *, child_session_id='', **kwargs)` | Parent-side completed delegation observation; optional no-op. Not a workflow lifecycle writer. |
| Hosted plugin bridge | `PluginContext`; category provider/proxy machinery | Can register providers and proxy provider methods for a core-owned manager. No context operation exposes a profile-owned workflow record writer with destination identity and receipts. |
| Hindsight write | `get_tool_schemas()`; `handle_tool_call('hindsight_retain', {'content': ..., ...}, **kwargs)` | Public tool path exists; context-only memory mode advertises no tools. Calling `_tool_retain`, `_retain_batch` or private client/bank attributes directly is rejected as the plugin integration strategy. |
| Destination identity | Provider `identity_signature()` | Cache identity mapping, callable before initialize. Not a stable post-initialize write-bank receipt or destination token. |
| Bank routing | Hindsight initialization and session switching | Trusted repository override, templates, static fallback, optional existing mirror/additional banks. Session/workspace/platform/user may change routing. Reconstructing this in the workflow plugin would diverge. |
| Automatic capture policy | `sync_turn`; Hindsight retain policy | Automatic scheduled capture is off unless `retain_cron` is enabled. Explicit retain tools are a different path. Do not treat automated summaries as fake primary chat turns to evade policy. |
| Redaction | Manager provider dispatch; plugin `redact.py` | Manager redacts tool arguments and hooks. Workflow records also need minimization and redaction before durable intent and before crossing any sink boundary. |
| Delivery acknowledgement | Hindsight public tool result | JSON wraps prose: queued for storage or stored successfully. No per-record receipt/operation ID exposed by that result; queued is not searchable. |
| Drain/visibility | `MemoryManager.flush_pending(timeout=None)`; provider `shutdown()` | Manager sentinel drains its executor, not necessarily provider queues or server jobs. Hindsight has its own bounded shutdown and private visibility polling. Neither is a generic durable/search-visible receipt. |
| Cancellation/timeout | Direct manager tool dispatch | Synchronous provider call has no generic per-record cancellation/deadline contract. A timed-out caller cannot conclude no write occurred. |

The current direct workflow worker constructs an `AIAgent(platform='workflow')`.
Normal agent memory may therefore operate under its worker profile/lifecycle.
That does not provide an orchestrator-owned writer or evidence that every worker
mode, provider and scheduled context captures the same data.

## Minimal host extension required

This was the required contract proposal at audit time. It is implemented only in
the prepared local extension, **not an API available in the running gateway**.
The host should offer a capability-negotiated record writer to plugins and
headless callers, backed by the configured provider's explicitly declared opt-in:

* A pure capability description, separate from health, activation and write
  consent. Unknown/older hosts return `capability_unavailable` with no fallback.
* A host-owned destination binding for the run-owner profile, provider and
  resolved write-bank set. Keep credentials private. Destination tokens must
  change when routing/selection changes and be validated atomically when a
  write is accepted. Reconnect/restart cannot silently move pending records.
* Bounded initialization, writes and cleanup, through the supported hosted
  boundary and scoped secrets. No new `AIAgent` or model call is needed.
* Explicit policy for manual/scheduled workflow writes, with consent distinct
  from automatic conversation capture. Preserve existing provider routing,
  including any already configured provider-side mirrors; add no plugin fanout.
* Typed receipts: unavailable/rejected, accepted/queued, confirmed durable,
  failed or unknown. Capability flags declare idempotency, receipt lookup and
  visibility support. A receipt must not assert recall visibility by inference.
* Stable client effect IDs where supported. Unknown deliveries require explicit
  reconciliation; never blindly resend after timeout, process death or ambiguous
  acknowledgement. A second fake provider must work through the same contract.

A public Hindsight retain-tool adapter can be implemented behind that host
boundary if it reports the actual acknowledgement limitations and supplies a
supported destination-binding contract. Existing private Hindsight fields are
not an acceptable substitute for the latter.

## Local outbox and configuration decision

SQLite remains the execution authority. The foundation uses the existing
`py_effects` ledger for immutable redacted records and delivery states, separate
from step execution and run leases. Record identity includes run, execution
revision, node, iteration and attempt where applicable. Scope and destination
are frozen at run creation; old revisions cannot start new external effects.
No historical backfill occurs on upgrade. An acknowledgement is not an
exactly-once or search-visibility guarantee.

Lifecycle scope: optional start and node settlement events, node failures,
terminal completed/failed/cancelled summary plus deterministic retrospective.
Raw input, prompts, output, review notes, transcripts, file paths, delivery
addresses and credentials are excluded from default external records. No LLM
summarization or new recall/prompt injection is introduced. Memory failures are
fail-open and must not change a successful workflow outcome.

The native policy will default to disabled. Enablement must explicitly select
the Hermes route; it must not change `memory.provider`, bank configuration or
credentials. Preserve the existing `open_second_brain` keys and their `auto`
meaning (O2B selection, not Hermes selection). Legacy inactive settings do not
imply consent to start writing. Unsupported strict/fail-closed settings must be
reported honestly rather than silently claiming enforcement. O2B remains a
separate optional adapter, without a mandatory Bun dependency.

Live conformance needs separately authorized writes to a named disposable
profile/bank, destination and receipt checks, and restoration evidence. This
audit made no live writes, configuration changes or provider activations.
