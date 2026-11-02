"""Read-only source audit with fake-provider probes; never import a real provider.

Usage: python scripts/audit_memory_contract.py HERMES_SOURCE HINDSIGHT_DIRECTORY
This reports source hashes, not provider health or a live integration result.
"""

import ast
import hashlib
import json
import sys
import types
from pathlib import Path


def audit(root, hindsight):
    report = {}
    for rel in (
        "agent/memory_provider.py",
        "agent/memory_manager.py",
        "plugins/memory/__init__.py",
        "hermes_cli/plugins.py",
        "hermes_cli/plugins_capability_ctx.py",
        "hermes_cli/plugin_host.py",
        "hermes_cli/plugin_host_child.py",
    ):
        source = (root / rel).read_bytes()
        tree = ast.parse(source)
        report[rel] = {
            "sha256": hashlib.sha256(source).hexdigest(),
            "memory_methods": sorted(
                {
                    n.name
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and any(s in n.name for s in ("memory", "record", "retain"))
                }
            ),
        }
    module = types.ModuleType("_workflow_memory_contract_audit")
    sys.modules[module.__name__] = module
    try:
        # This base-contract module imports only the standard library.
        path = root / "agent/memory_provider.py"
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)

        class Fake(module.MemoryProvider):
            name = "fake"

            def is_available(self):
                raise AssertionError("must not probe a real provider")

            def initialize(self, *args, **kwargs):
                raise AssertionError("must not initialize")

            def get_tool_schemas(self):
                return []

        fake = Fake()
        assert fake.on_memory_write("add", "memory", "audit") is None
        assert fake.on_delegation("audit", "result") is None
        assert not any(
            hasattr(fake, name)
            for name in ("write_record", "retain", "write_event", "write_retrospective")
        )
    finally:
        sys.modules.pop(module.__name__, None)

    path = hindsight / "__init__.py"
    source = path.read_bytes()
    report["hindsight/__init__.py"] = {"sha256": hashlib.sha256(source).hexdigest()}
    cls = next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.ClassDef) and n.name == "HindsightMemoryProvider"
    )
    method = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "handle_tool_call"
    )
    namespace = {
        "json": json,
        "logger": types.SimpleNamespace(warning=lambda *args, **kwargs: None),
        "tool_error": lambda message: json.dumps({"error": message}),
    }
    # Execute only the public dispatch function with injected fake handlers.
    # No Hindsight imports, client construction, configuration or secret reads.
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    for answer in ("Memory queued for storage.", "Memory stored successfully."):
        fake = types.SimpleNamespace(
            _TOOL_HANDLERS={
                "hindsight_retain": ("content", lambda self, args, result=answer: result, "Failed")
            }
        )
        dispatch = namespace["handle_tool_call"]
        assert json.loads(dispatch(fake, "hindsight_retain", {"content": "audit"})) == {
            "result": answer
        }
        assert "error" in json.loads(dispatch(fake, "hindsight_retain", {}))
    report["probes"] = {
        "base_hooks": "no-op; no arbitrary-record API",
        "public_hindsight_dispatch": "queued/stored prose results; missing content rejects",
        "real_provider_initializations": 0,
        "network_calls": 0,
    }
    return report


if __name__ == "__main__":
    print(json.dumps(audit(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
