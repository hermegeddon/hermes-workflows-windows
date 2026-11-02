"""Isolated native memory bridge probe with a real provider class and fake SDK.

Pass --hermes-source and --provider-source (the prepared Hindsight contribution).
No provider configuration, credentials or bank outside temporary homes is used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

SDK_FIXTURE = """
# Probe-only SDK boundary: no HTTP client or external service is constructed.
from types import SimpleNamespace
class _ProbeSDK:
    def __init__(self, home): self.home = home
    async def aretain_batch(self, **kwargs):
        with (self.home / 'sdk-records.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(kwargs) + '\\n')
        return SimpleNamespace(operation_id='probe-queued' if kwargs.get('retain_async') else None)
    async def aclose(self): pass
class _ProbeHindsight(HindsightMemoryProvider):
    def is_available(self): return True
    def _new_cloud_client(self): return _ProbeSDK(get_hermes_home())
def register(ctx): ctx.register_memory_provider(_ProbeHindsight())
"""

CONSUMER = """
import json
from pathlib import Path
from hermes_constants import get_hermes_home
def register(ctx):
    def check(args, **kwargs):
        from check_memory_bridge import exercise
        return json.dumps(exercise(Path(get_hermes_home()), context=ctx, scheduled=args.get('scheduled', False)))
    ctx.register_tool(name='memory_bridge_probe', toolset='memory_bridge_probe',
        schema={'name': 'memory_bridge_probe', 'description': 'Isolated memory probe',
                'parameters': {'type': 'object', 'properties': {}}}, handler=check)
"""


def exercise(home, *, context=None, scheduled=False):
    from hermes_workflows.db import Database
    from hermes_workflows.engine import Engine
    from hermes_workflows.memory_service import MemoryService

    run_id = f"bridge-{uuid.uuid4().hex}"
    db = Database(home / "workflows/runs.db")
    db.save_settings(
        {
            "workflow_memory_enabled": True,
            "workflow_memory_allow_scheduled": True,
            "workflow_memory_write_node_events": True,
            "workflow_memory_timeout_seconds": 15,
        }
    )
    service = MemoryService(db, context=context)
    engine = Engine(db, memory_policy=service)
    run = engine.start(
        {
            "id": run_id,
            "name": "Memory bridge QA",
            "version": 1,
            "scope": {"type": "global"},
            "trigger": {"type": "manual"},
            "nodes": [{"id": "done", "type": "finish"}],
            "edges": [],
        },
        run_id=run_id,
        scheduled=scheduled,
    )
    assert db.claim(run_id, "bridge-probe")
    asyncio.run(engine.drive(run_id, "bridge-probe"))
    assert db.get_run(run_id)["status"] == "completed"
    outbox = service.outbox
    entries = outbox.entries(run["run_id"])
    assert len(entries) == 4, entries
    asyncio.run(service.dispatch())
    statuses = sorted({entry["status"] for entry in outbox.entries(run_id)})
    assert service.status()["last_delivery"]["status"] == "queued"
    return {
        "run_id": run_id,
        "records": len(entries),
        "statuses": statuses,
        "scheduled": scheduled,
        "provider": run["memory_export"]["destination"]["provider"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--provider-source", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    host = args.hermes_source.resolve()
    provider = args.provider_source.resolve()
    root = Path(__file__).resolve().parents[1]
    paths = [str(host), str(root / "hermes-workflows-py"), str(root / "scripts")]
    sys.path[:0] = paths
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    reports = []
    with tempfile.TemporaryDirectory(prefix="hermes-memory-bridge-") as temporary:
        temp = Path(temporary)
        os.environ["HERMES_HOME"] = str(temp / "unused-launch-home")
        from hermes_cli import plugins as plugins_module
        from hermes_cli.plugins import PluginManager
        from hermes_cli.plugins_loader import _plugin_home_scope
        from tools.registry import registry

        empty_bundled = temp / "empty-bundled"
        empty_bundled.mkdir()
        plugins_module.get_bundled_plugins_dir = lambda: empty_bundled

        for isolation in ("in_process", "host"):
            home = temp / isolation
            home.mkdir()
            config = {
                "memory": {"provider": "hindsight"},
                "plugins": {
                    "enabled": ["hermes-workflows"],
                    "isolation": isolation,
                    "entries": {
                        "hermes-workflows": {
                            "granted_capabilities": [
                                "memory.write_records",
                                "memory.write_scheduled_records",
                            ]
                        }
                    },
                },
            }
            # JSON is valid YAML, so the native config backend reads this normally.
            (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
            directory = home / "plugins/hindsight"
            directory.mkdir(parents=True)
            for path in provider.glob("*.py"):
                if path.name != "setup.py":
                    (directory / path.name).write_bytes(path.read_bytes())
            with (directory / "__init__.py").open("a", encoding="utf-8") as stream:
                stream.write(SDK_FIXTURE)
            (directory / "plugin.yaml").write_text(
                "name: hindsight\nversion: 1.2.3\n", encoding="utf-8"
            )
            config_path = home / "hindsight/config.json"
            config_path.parent.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "mode": "cloud",
                        "api_url": "http://127.0.0.1:9/unused-fake-sdk",
                        "apiKey": "synthetic-key",
                        "bank_id": "qa-owner",
                        "additional_banks": ["qa-mirror"],
                        "retain_async": True,
                    }
                ),
                encoding="utf-8",
            )
            consumer = home / "plugins/hermes-workflows"
            consumer.mkdir()
            (consumer / "__init__.py").write_text(CONSUMER, encoding="utf-8")
            (consumer / "plugin.yaml").write_text(
                json.dumps(
                    {
                        "name": "hermes-workflows",
                        "version": "1.0",
                        "capabilities": [
                            "memory.write_records",
                            "memory.write_scheduled_records",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with _plugin_home_scope(home):
                manager = PluginManager()
                manager.discover_and_load()
                try:
                    for scheduled in (False, True):
                        report = json.loads(
                            registry.dispatch(
                                "memory_bridge_probe",
                                {"scheduled": scheduled},
                                scope=manager.scope_key,
                            )
                        )
                        assert report.get("records") == 4, report
                        assert report["statuses"] == ["queued"], report
                        reports.append({"isolation": isolation, "path": "ctx", **report})
                    reports.append({"isolation": isolation, "path": "headless", **exercise(home)})
                    sdk_rows = [
                        json.loads(line)
                        for line in (home / "sdk-records.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                    ]
                    assert len(sdk_rows) == 24, len(sdk_rows)
                    assert {row["bank_id"] for row in sdk_rows} == {"qa-owner", "qa-mirror"}
                finally:
                    manager.unload()
                    host_instance = getattr(manager, "_plugin_host_instance", None)
                    if host_instance is not None:
                        host_instance.shutdown()
                    from hermes_cli.plugins import get_plugin_manager

                    shared = getattr(get_plugin_manager(), "_plugin_host_instance", None)
                    if shared is not None:
                        shared.shutdown()
    report = {
        "checks": reports,
        "real_service_requests": 0,
        "model_calls": 0,
        "production_config_changes": 0,
    }
    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
