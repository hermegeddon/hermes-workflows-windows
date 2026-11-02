"""Verify the installed Hermes APIs without invoking its update/bootstrap command.

By default this uses temporary workflow storage and makes no provider calls.
--live-agent additionally permits one bounded request to the configured provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import site
import sys
import tempfile
import time
import types
from pathlib import Path


def activate(source: Path):
    sys.path.insert(0, str(source))
    from pm.environments import committed_venv, site_packages

    environment = committed_venv(source)
    if environment is None:
        raise RuntimeError("Hermes has no prepared dependency environment")
    site.addsitedir(str(site_packages(environment)))


def host_contract(plugin: Path) -> dict:
    import importlib

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

    with tempfile.TemporaryDirectory(prefix="workflow native contract ") as temporary:
        home = Path(temporary)
        # Discovery deduplicates by plugin name. Isolate the selected package so
        # an adjacent upstream checkout cannot be mistaken for this backend.
        selected = home / "plugins" / "hermes-workflows"
        shutil.copytree(plugin, selected, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        plugin = selected
        token = set_hermes_home_override(home)
        manager = None
        canonical = sys.modules.get("hermes_workflows")
        poisoned = types.ModuleType("hermes_workflows")
        poisoned.__file__ = str(home / "older-profile/hermes_workflows/__init__.py")

        def wrong_register(ctx):
            raise AssertionError("selected another installation's canonical backend")

        poisoned.register = wrong_register
        sys.modules["hermes_workflows"] = poisoned
        try:
            (home / "config.yaml").write_text(
                "plugins:\n  isolation: in_process\n", encoding="utf-8"
            )
            manifest = parse_manifest_file(plugin / "plugin.yaml", plugin, "user", "")
            if manifest is None:
                raise RuntimeError("Hermes rejected the plugin manifest")
            manager = PluginManager()
            entry_module = manager._load_directory_module(manifest)
            entry_module.register(PluginContext(manifest, manager))
            get_runtime = importlib.import_module(
                entry_module.register.__module__ + ".runtime"
            ).get_runtime
            runtime = get_runtime(home)
            assert runtime.thread.is_alive()
            assert len(manager._plugin_tool_names) == 6
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
                    portable = {
                        **wf, "id": "native-template", "name": "Native portable template",
                        "defaults": {"profile": "source-worker", "model": "source-model@provider"},
                        "nodes": [{"id": "work", "type": "agent_task", "prompt": "Plan the change.",
                                   "workdir": str(home / "source workspace"), "skills": ["required-skill"]},
                                  {"id": "done", "type": "finish"}],
                        "edges": [{"from": "work", "to": "done"}],
                    }
                    response = await client.post(base + "/workflows", json={"workflow": portable})
                    assert response.status_code == 200, response.text
                    response = await client.get(base + "/workflows/native-template/export-template")
                    assert response.status_code == 200, response.text
                    bundle = response.json()
                    assert not bundle["cached"] and bundle["version"]["template_format"] == 1
                    document = json.loads(bundle["yaml"])
                    assert document["defaults"]["profile"].startswith("${PROFILE:")
                    assert document["nodes"][0]["workdir"] == "${WORKDIR}"
                    assert "source-model" not in bundle["yaml"]
                    assert "required-skill" in bundle["md"] and "Prerequisites" in bundle["md"]
                    assert (await client.get(base + "/workflows/native-template/export-template")).json() == {
                        **bundle, "cached": True,
                    }
                    document.pop("template")
                    document["id"] = "native-adapted-template"
                    document["defaults"] = {"profile": "target-worker", "model": "target-model"}
                    document["nodes"][0]["workdir"] = str(home / "target workspace")
                    response = await client.post(base + "/workflows", json={"workflow": document})
                    assert response.status_code == 200, response.text
                    assert not (await client.get(base + "/runs?scope=all&workflow_id=native-template")).json()["runs"]
                    response = await client.get(base + "/runs/" + run_id + "/attempts")
                    assert response.status_code == 200 and response.json()["attempts"], (
                        response.text
                    )
                    response = await client.post(
                        base + "/runs/" + run_id + "/approvals/absent-request",
                        json={"node_id": "gate", "attempt_id": "absent-attempt", "choice": "once"},
                    )
                    assert (
                        response.status_code == 404
                        and response.json()["detail"] == "command approval not found"
                    ), response.text

                    loop = {
                        "id": "native-loop",
                        "name": "Native loop",
                        "version": 1,
                        "scope": {"type": "global"},
                        "trigger": {"type": "manual"},
                        "nodes": [
                            {"id": "start", "type": "prompt"},
                            {"id": "review", "type": "human_review"},
                            {"id": "again", "type": "condition"},
                            {"id": "done", "type": "finish"},
                        ],
                        "edges": [
                            {"from": "start", "to": "review"},
                            {
                                "from": "review",
                                "to": "again",
                                "condition": {"type": "review_status", "equals": "needs_changes"},
                            },
                            {"from": "again", "to": "review"},
                            {
                                "from": "review",
                                "to": "done",
                                "condition": {"type": "review_status", "equals": "approved"},
                            },
                        ],
                    }
                    response = await client.post(base + "/workflows", json={"workflow": loop})
                    assert response.status_code == 200, response.text
                    response = await client.post(base + "/workflows/native-loop/run", json={})
                    assert response.status_code == 200, response.text
                    loop_id = response.json()["run_id"]

                    async def wait_loop(predicate):
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            response = await client.get(base + "/runs/" + loop_id)
                            assert response.status_code == 200, response.text
                            data = response.json()
                            if predicate(data):
                                return data
                            await asyncio.sleep(0.03)
                        raise AssertionError(data)

                    first = await wait_loop(lambda run: run["status"] == "waiting")
                    old_token = first["nodes"]["review"]["attempt_id"]
                    review_url = base + "/runs/" + loop_id + "/review"
                    response = await client.post(
                        review_url,
                        json={
                            "node_id": "review",
                            "decision": "needs_changes",
                            "attempt_id": old_token,
                        },
                    )
                    assert response.status_code == 200, response.text
                    second = await wait_loop(
                        lambda run: (
                            run["status"] == "waiting"
                            and run["nodes"]["review"].get("attempt_id") != old_token
                        )
                    )
                    response = await client.post(
                        review_url,
                        json={"node_id": "review", "decision": "approved", "attempt_id": old_token},
                    )
                    assert response.status_code == 409, response.text
                    response = await client.post(
                        review_url,
                        json={
                            "node_id": "review",
                            "decision": "approved",
                            "attempt_id": second["nodes"]["review"]["attempt_id"],
                        },
                    )
                    assert response.status_code == 200, response.text
                    await wait_loop(lambda run: run["status"] == "completed")

                    # Exercise resume through the real directory-loaded router
                    # with a capture-only agent callback and a terminal fixture.
                    resumable = {
                        "id": "native-resume",
                        "name": "Native resume",
                        "version": 1,
                        "scope": {"type": "global"},
                        "trigger": {"type": "manual"},
                        "defaults": {"profile": "capture-only"},
                        "nodes": [
                            {"id": "prefix", "type": "condition"},
                            {"id": "work", "type": "agent_task", "prompt": "capture only"},
                            {"id": "done", "type": "finish"},
                        ],
                        "edges": [{"from": "prefix", "to": "work"}, {"from": "work", "to": "done"}],
                    }
                    response = await client.post(base + "/workflows", json={"workflow": resumable})
                    assert response.status_code == 200, response.text
                    state = {
                        "run_id": "native-resume-run",
                        "workflow_id": resumable["id"],
                        "workflow_version": 1,
                        "status": "failed",
                        "started_at": time.time(),
                        "finished_at": time.time(),
                        "params": {},
                        "nodes": {
                            "prefix": {
                                "node_id": "prefix",
                                "node_type": "condition",
                                "status": "completed",
                                "outcome": "success",
                                "output": "preserved",
                                "seq": 1,
                            },
                            "work": {
                                "node_id": "work",
                                "node_type": "agent_task",
                                "status": "completed",
                                "outcome": "failure",
                                "seq": 2,
                            },
                            "done": {
                                "node_id": "done",
                                "node_type": "finish",
                                "status": "skipped",
                                "seq": 3,
                            },
                        },
                    }
                    before = runtime.db.create_run(state, resumable)
                    result_class = importlib.import_module(
                        entry_module.register.__module__ + ".executor"
                    ).Result

                    async def capture_only(node, run):
                        return result_class(output="native resume capture")

                    previous_runner = runtime.engine.executor.agent_runner
                    runtime.engine.executor.agent_runner = capture_only
                    try:
                        response = await client.post(
                            base + "/runs/native-resume-run/resume", json={}
                        )
                        assert response.status_code == 200, response.text
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            response = await client.get(base + "/runs/native-resume-run")
                            assert response.status_code == 200, response.text
                            if response.json()["status"] == "completed":
                                break
                            await asyncio.sleep(0.03)
                        else:
                            raise AssertionError(response.text)
                        assert response.json()["nodes"]["prefix"] == before["nodes"]["prefix"]
                        assert response.json()["nodes"]["work"]["output"] == "native resume capture"
                        response = await client.post(
                            base + "/runs/native-resume-run/resume", json={}
                        )
                        assert response.status_code == 409, response.text
                        response = await client.put(
                            base + "/settings", json={"default_mode": "direct"}
                        )
                        assert response.status_code == 200, response.text
                        response = await client.post(
                            base + "/workflows/native-resume/compile-preview", json={}
                        )
                        assert response.json()["execution"] == {
                            "mode": "direct",
                            "inline_scripts": True,
                        }, response.text
                    finally:
                        runtime.engine.executor.agent_runner = previous_runner

            asyncio.run(check_routes())
            manager.unload(manifest_key(manifest))
            assert not runtime.thread.is_alive()
            assert not manager._plugin_tool_names
            return {
                "plugin_registration": "passed",
                "directory_import_isolation": "passed",
                "unload": "passed",
                "native_dashboard_mount": "passed",
                "dashboard_run_review": "passed",
                "dashboard_loop_review": "passed",
                "dashboard_resume_and_mode": "passed",
                "dashboard_portable_template": "passed",
                "dashboard_worker_observation_routes": "passed",
            }
        finally:
            if manager is not None:
                manager.unload()
            if canonical is None:
                sys.modules.pop("hermes_workflows", None)
            else:
                sys.modules["hermes_workflows"] = canonical
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
    parser.add_argument(
        "--capabilities", action="store_true", help="probe native APIs without running workflows"
    )
    args = parser.parse_args()
    activate(args.hermes_source)
    sys.path.insert(0, str(args.plugin.resolve()))
    if args.capabilities:
        from hermes_cli.plugins import PluginContext
        from hermes_workflows.native import NativeHost

        with tempfile.TemporaryDirectory(prefix="workflow native capabilities ") as temporary:
            report = NativeHost(Path(temporary), context=PluginContext).probe()
        print(json.dumps(report, indent=2))
        return
    report = host_contract(args.plugin)
    print(json.dumps(report), flush=True)
    if args.agent_preflight:
        report.update(live_agent(home, preflight=True))
    if args.live_agent:
        report.update(live_agent(home))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
