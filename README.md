# hermes-workflows-py

A Python 3.11+ backend for the Hermes Workflows dashboard, built for native Windows.

The copyable plugin is in [`hermes-workflows-py/`](hermes-workflows-py/). See its
[installation and usage guide](hermes-workflows-py/README.md) for setup, API examples,
execution semantics, and compatibility limits.

Run the core suite from this repository with no third-party packages:

```powershell
python -m unittest discover -s tests -v
```

The optional dashboard integration test runs when FastAPI, httpx, and PyYAML are
available in the selected interpreter. Provider calls are replaced with deterministic
fixtures; no tests consume LLM credits or modify the real Hermes home.

The dashboard companion's loader tests use Node's built-in test runner (Node is
only needed for this development check):

```powershell
node --test tests/dashboard_entry.test.cjs
```

To check an already-running installation after a gateway restart:

```powershell
python .\scripts\check_running_backend.py --home $env:HERMES_HOME
```

This separate live check creates two uniquely named test workflows in that home,
approves a review, and waits for the existing supervisor and scheduler to complete
them. It does not start its own supervisor or call an LLM. It retains the test run
history and disables the test schedule when finished.
