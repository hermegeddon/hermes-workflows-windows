# Prepared Hermes host reload correction

`plugin-asgi-reload.patch` contains an isolated contribution against
`NousResearch/hermes-agent` commit
`50d2adf1cd070b178eb454d86c794f72933b9518`. It is independent of the memory
provider extension. Applying the artifact does not restart a running host.

The patch invalidates the unloaded directory plugin's cached ASGI apps and
prevents late imports from repopulating the cache with an old router. It keeps
unrelated warm apps intact. Its tests exercise actual hosted plugins in
temporary homes, including different dashboard/backend IDs, failing unload
callbacks and a cold import crossing unload. A shared synthetic JS asset writer
also uses explicit LF to satisfy its existing exact-byte contract on Windows.

Secondary-profile dashboards always run in a plugin host, including when the
backend runs in-process or the package has no backend. Manager unload therefore
also invalidates cached routers in its existing host: all on full rediscovery,
or only the owning directories on targeted unload. It never starts a host just
to invalidate it. Invalidation follows registration disposal and prevents a late
import from restoring stale state.

Six qualification modules finish with 83 passed and 5 expected platform skips
on native Windows/Python 3.14, including real secondary-profile HTTP requests.
All 14 canonical source checks pass. Negative controls and measurement limits
are described in the [investigation report](../windows-latency-investigation.md).

To review in an isolated checkout of that base:

```powershell
git apply --check <path-to-plugin-asgi-reload.patch>
git apply <path-to-plugin-asgi-reload.patch>
```

Use the host's canonical `scripts/run_tests.sh` for these modules:

```text
tests/hermes_cli/test_plugin_host_asgi_reload.py
tests/hermes_cli/test_plugin_host.py
tests/hermes_cli/test_plugin_host_route_auth.py
tests/hermes_cli/test_plugin_secondary_profile_host.py
tests/hermes_cli/test_plugin_api_consent_gate.py
tests/hermes_cli/test_plugin_ownership_ledger.py
```

The artifact also passes `git apply --check` against the isolated memory-host
extension worktree. Applying source, publishing the contribution and deploying
the host remain separate actions; a workflow plugin restart alone does not
install this host correction.
