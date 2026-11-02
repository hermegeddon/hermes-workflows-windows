# Native workflow memory

Workflow history and execution state remain in SQLite. Optional memory records
send minimized lifecycle facts to the run owner's existing Hermes memory
provider. They do not replace conversation memory, add recall to prompts, or
create an agent or a model request. Normal worker conversation capture remains
under its existing provider policy.

## Availability and consent

This feature needs the additive host contract described in
[the audit](memory-provider-contract.md), plus a provider that implements it.
The isolated Hermes extension and companion
[Hindsight contribution](provider-patches/README.md) are prepared for review;
neither is deployed to the running gateway. Older hosts report
`capability_unavailable`. There is no fallback to provider internals, another
store, MEMORY.md, or USER.md.

Selecting `memory.provider: hindsight` is not permission for additional workflow
writes. The owning profile must also grant the workflow plugin
`memory.write_records` through Hermes' normal capability consent flow. Scheduled
runs additionally require `memory.write_scheduled_records`. Provider credentials,
bank configuration and `memory.provider` are never rewritten by this feature.

## Policy

Fresh installs and upgrades are disabled by default. In the dashboard's
**Workflow memory** settings, enable workflow records and select the `hermes`
route. The stored global keys are:

| Setting | Default |
| --- | --- |
| `workflow_memory_enabled` | `false` |
| `workflow_memory_provider` | `hermes` (`none` disables) |
| `workflow_memory_write_run_summaries` | `true` |
| `workflow_memory_write_node_failures` | `true` |
| `workflow_memory_write_node_events` | `false` |
| `workflow_memory_allow_scheduled` | `false` |
| `workflow_memory_timeout_seconds` | `5` (positive integer, at most 30) |

Per-workflow values override these defaults under a separate extension:

```json
{
  "defaults": {
    "workflow_memory": {
      "enabled": true,
      "provider": "hermes",
      "write_run_summaries": true,
      "write_node_failures": true,
      "write_node_events": false,
      "allow_scheduled": false
    }
  }
}
```

`enabled: false` or `provider: none` explicitly disables writes. Setting all three
write flags false is also a no-op. A per-workflow opt-in does not require the
global default to be enabled, but still requires host permission and provider
support. Scheduled execution is marked by the scheduler, not inferred from a
workflow's trigger type. Strict/fail-closed mode is unsupported: requesting
`fail_open: false` reports `strict_mode_unsupported` without writing.

The existing `defaults.memory`, O2B settings and environment flags keep their old
meaning. If the native extension is absent, an explicit legacy provider `none`
remains an opt-out; `auto` and `open_second_brain` never become aliases for
Hermes. Legacy settings alone do not enable new writes. If both policy objects
exist, the explicit native object controls native writes independently. JSON and
template exports preserve both. The upstream TypeScript backend does not
implement `workflow_memory`; review its separate legacy policy before using an
export there. No new environment-variable enablement is introduced.

## Records and delivery

Only new opted-in runs capture records. Enabling the feature does not backfill
history or modify previous run state. The provider, owner profile, resolved
workspace/origin context and destination binding are frozen at run creation.
Worker-profile overrides do not redirect the orchestrator's records. A provider
or destination change rejects pending delivery rather than sending elsewhere.

Default records contain terminal status, minimized failure facts and a
deterministic retrospective. Optional events add run start and node settlement.
They exclude raw input, prompts, output, error text, review notes, transcripts,
file paths and chat delivery addresses. Identifiers are clipped/redacted, and
the host applies its redactor again before provider dispatch. Stable effect
identities include execution revision, node, iteration and attempt where
applicable. These are separate explicit facts, not synthetic conversation turns.

Run mutations and outbox intent commit together in the existing `py_effects`
ledger; delivery uses its own ownership/deadline and cannot hold the workflow
driver lease. Memory errors remain fail-open. Turning off the effective policy
retires prepared effects; re-enabling does not resend them. Crashed/timed-out
in-flight delivery becomes unknown, never an automatic retry.

| State | Meaning |
| --- | --- |
| `prepared` / `in_flight` | Local intent / dispatched, without acceptance proof |
| `queued` | Provider accepted asynchronous processing; durability and recall visibility are unconfirmed |
| `acknowledged` | Provider reports durable storage; this does not prove search visibility |
| `failed` / `unavailable` | Rejected or unsupported; safe reason codes only |
| `unknown` | Acceptance is ambiguous; never resend automatically |

The prepared Hindsight implementation preserves its existing write-bank set,
mirrors and retain shaping. It advertises neither server-side effect deduplication
nor receipt lookup. Its asynchronous receipts therefore stay queued; a green
configuration probe is not a retention/readback test.

## Inspection and validation

Web and Desktop share the Workflow memory settings panel. Configuration status
does not initialize a provider. Verified capabilities are labeled as the last
run's observation, and delivery state is shown separately. The API adds
`GET /memory-status`, `GET /runs/{id}/memory` and
`POST /runs/{id}/memory/{effect_id}/reconcile`. Reconciliation is read-only; it
cannot resend an effect. Unsupported lookup preserves an existing receipt.
Settings responses retain all legacy fields and add optional memory metadata;
compile preview similarly adds `native_memory` without changing the pinned
upstream memory enum.

`scripts/check_memory_bridge.py` exercises the prepared host, actual Hindsight
class and workflow settings/outbox across in-process, hosted and headless paths.
Its injected SDK uses temporary profiles and performs no service requests.
Automated evidence is distinct from live acceptance. Deployment, physical
Desktop acceptance and a real retained-record/readback check in a named
disposable profile/bank require a separately authorized step. The current work
does not update or restart the gateway or touch the user's existing memory bank.
