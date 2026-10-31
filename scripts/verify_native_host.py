"""Verify the installed Hermes APIs without invoking its update/bootstrap command.

By default this uses temporary workflow storage and makes no provider calls.
--live-agent additionally permits one bounded request to the configured provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import site
import sys
import tempfile
import time
from pathlib import Path


def activate(source: Path):
    sys.path.insert(0, str(source))
    from pm.environments import committed_venv, site_packages

    environment = committed_venv(source)
    if environment is None:
        raise RuntimeError("Hermes has no prepared dependency environment")
    site.addsitedir(str(site_packages(environment)))


def host_contract(plugin: Path) -> dict:
    import httpx
    from fastapi import FastAPI
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import manifest_key, parse_manifest_file
    from hermes_cli.web_server_dashboard import (
        _discover_dashboard_plugins,
        _mount_one_plugin_api,
        _plugin_route_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_workflows import register
    from hermes_workflows.runtime import get_runtime

    with tempfile.TemporaryDirectory(prefix="workflow native contract ") as temporary:
        home = Path(temporary)
        token = set_hermes_home_override(home)
        manager = None
        try:
            (home / "config.yaml").write_text(
                "plugins:\n  isolation: in_process\n", encoding="utf-8"
            )
            manifest = parse_manifest_file(plugin / "plugin.yaml", plugin, "user", "")
            if manifest is None:
                raise RuntimeError("Hermes rejected the plugin manifest")
            manager = PluginManager()
            register(PluginContext(manifest, manager))
            runtime = get_runtime(home)
            assert runtime.thread.is_alive()
            assert len(manager._plugin_tool_names) == 5
            assert "workflow" in manager._plugin_commands
            # Use native dashboard discovery and route mounting. Authentication
            # alone is overridden in this isolated, in-memory test application.
            entries = _discover_dashboard_plugins([(plugin.parent, "user")])
            entry = next(p for p in entries if p["name"] == "hermes-workflows")
            application = FastAPI()
            application.dependency_overrides[_plugin_route_secret_scope] = lambda: None
            assert _mount_one_plugin_api(application, entry, {"hermes-workflows"}, set())

            async def check_routes():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application), base_url="http://test"
                ) as client:
                    base = "/api/plugins/hermes-workflows"
                    wf = {
                        "id": "native-test",
                        "name": "Native test",
                        "version": 1,
                        "scope": {"type": "global"},
                        "trigger": {"type": "manual"},
                        "nodes": [
                            {"id": "gate", "type": "human_review"},
                            {"id": "done", "type": "finish"},
                        ],
                        "edges": [
                            {
                                "from": "gate",
                                "to": "done",
                                "condition": {"type": "review_status", "equals": "approved"},
                            }
                        ],
                    }
                    response = await client.post(base + "/workflows", json={"workflow": wf})
                    assert response.status_code == 200, response.text
                    response = await client.post(base + "/workflows/native-test/run", json={})
                    assert response.status_code == 200, response.text
                    run_id = response.json()["run_id"]
                    for target in ("waiting", "completed"):
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            response = await client.get(base + "/runs/" + run_id)
                            assert response.status_code == 200, response.text
                            if response.json()["status"] == target:
                                break
                            await asyncio.sleep(0.03)
                        else:
                            raise AssertionError(response.text)
                        if target == "waiting":
                            response = await client.post(
                                base + "/runs/" + run_id + "/review",
                                json={"node_id": "gate", "decision": "approved"},
                            )
                            assert response.status_code == 200, response.text
                    assert (
                        len(
                            (
                                await client.get(base + "/runs?scope=all&workflow_id=native-test")
                            ).json()["runs"]
                        )
                        == 1
                    )
                    assert (await client.get(base + "/runs?scope=all&workflow_id=missing")).json()[
                        "runs"
                    ] == []
                    assert (
                        await client.get(base + "/runs/" + run_id + "/export")
                    ).status_code == 200
                    assert (await client.get(base + "/settings")).status_code == 200

            asyncio.run(check_routes())
            manager.unload(manifest_key(manifest))
            assert not runtime.thread.is_alive()
            assert not manager._plugin_tool_names
            return {
                "plugin_registration": "passed",
                "unload": "passed",
                "native_dashboard_mount": "passed",
                "dashboard_run_review": "passed",
            }
        finally:
            if manager is not None:
                manager.unload()
            reset_hermes_home_override(token)


def live_agent(home: Path, *, preflight=False) -> dict:
    from hermes_workflows.db import Database
    from hermes_workflows.executor import Executor
    from hermes_workflows.runtime import Runtime
    from hermes_workflows.specs import SpecStore

    with tempfile.TemporaryDirectory(prefix="workflow real agent ") as temporary:
        root = Path(temporary)
        runtime = Runtime(
            Database(root / "runs.db"), SpecStore([root / "specs"]), Executor(home=home)
        )
        wf = {
            "id": "live-agent-probe",
            "name": "Bounded live agent probe",
            "version": 1,
            "scope": {"type": "global"},
            "trigger": {"type": "manual"},
            "edges": [],
            "nodes": [
                {
                    "id": "reply",
                    "type": "agent_task",
                    "profile": "default",
                    "prompt": "This is a connectivity test. Reply with exactly WORKFLOW_SMOKE_OK. Do not use tools.",
                    "enabled_toolsets": [],
                    "max_iterations": 2,
                    "max_tokens": 2048,
                    "load_external_secrets": False,
                    "_preflight": preflight,
                    "workdir": str(root),
                    "timeout_seconds": 90,
                }
            ],
        }
        runtime.start()
        try:
            state = runtime.engine.start(wf)
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                state = runtime.db.get_run(state["run_id"])
                if state["status"] in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.1)
            node = state["nodes"]["reply"]
            # Do not print worker logs or arbitrary provider responses: they can
            # contain private local configuration. Only the exact marker is shown.
            marker = "WORKFLOW_PREFLIGHT_OK" if preflight else "WORKFLOW_SMOKE_OK"
            if state["status"] != "completed" or marker not in node.get("output", ""):
                raise RuntimeError(
                    "live agent probe failed: "
                    + json.dumps(
                        {
                            "status": state["status"],
                            "outcome": node.get("outcome"),
                            "output_length": len(node.get("output", "")),
                            "marker_present": marker in node.get("output", ""),
                            "error": node.get("error"),
                            "telemetry": node.get("telemetry", {}),
                        }
                    )
                )
            return {
                "agent_preflight" if preflight else "live_agent": "passed",
                "output": marker,
                "telemetry": node.get("telemetry", {}),
            }
        finally:
            runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    parser.add_argument("--hermes-source", type=Path, default=home / "hermes-agent")
    parser.add_argument("--plugin", type=Path, default=home / "plugins" / "hermes-workflows")
    parser.add_argument("--live-agent", action="store_true")
    parser.add_argument("--agent-preflight", action="store_true")
    args = parser.parse_args()
    activate(args.hermes_source)
    sys.path.insert(0, str(args.plugin.resolve()))
    report = host_contract(args.plugin)
    print(json.dumps(report), flush=True)
    if args.agent_preflight:
        report.update(live_agent(home, preflight=True))
    if args.live_agent:
        report.update(live_agent(home))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
