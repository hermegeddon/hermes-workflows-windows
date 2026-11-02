from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-workflows-py"
sys.path.insert(0, str(PLUGIN))
from hermes_workflows import register
from hermes_workflows.api import API, BASE, ROUTES, ASGIApp
from hermes_workflows.db import ConflictError, Database
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Executor, Result, run_process
from hermes_workflows.runtime import Runtime
from hermes_workflows.schedules import Cron
from hermes_workflows.settings import save
from hermes_workflows.specs import SpecStore, fill_params, validate


def workflow(nodes=None, edges=None, id="demo"):
    return {
        "id": id,
        "name": "Demo workflow",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "default"},
        "nodes": nodes or [{"id": "finish", "type": "finish"}],
        "edges": edges or [],
    }


def agent(name, **extra):
    return {"id": name, "type": "agent_task", "prompt": name, **extra}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow test żółć ")
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "state folder" / "runs.db")
        self.specs = SpecStore([self.root / "global"])
        self.runtimes = []
        save(self.db, {"scripts_enabled": True})

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close()
        self.temp.cleanup()

    def runtime(self, runner=None):
        runtime = Runtime(self.db, self.specs, Executor(agent_runner=runner))
        self.runtimes.append(runtime)
        return runtime

    def wait(self, run_id, statuses=("completed", "failed", "cancelled", "waiting"), timeout=8):
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            state = self.db.get_run(run_id)
            if state["status"] in statuses:
                return state
            time.sleep(0.02)
        diagnostics = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "run": self.db.get_run(run_id),
            "attempts": self.db.attempts(run_id),
            "trace_tail": self.db.trace(run_id).splitlines()[-10:],
            "runtimes": [
                {
                    "owner": runtime.owner,
                    "thread_alive": bool(runtime.thread and runtime.thread.is_alive()),
                    "startup_error": repr(runtime._startup_error),
                }
                for runtime in self.runtimes
            ],
        }
        self.fail(f"run did not reach {statuses}: " + json.dumps(diagnostics, default=str))

    def test_wal_single_flight_and_threaded_reads(self):
        with self.db.connect() as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        engine = Engine(self.db)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(engine.start, workflow()) for _ in range(8)]
            results = []
            for f in futures:
                try:
                    results.append(f.result())
                except ConflictError:
                    pass
        self.assertEqual(len(results), 1)
        run_id = results[0]["run_id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(self.db.get_run, [run_id] * 24))
        self.assertTrue(all(s["run_id"] == run_id for s in states))

    def test_parallel_fan_in_prompt_mapping_and_params(self):
        completed, prompts = [], {}
        running, peak = 0, 0
        parallel_ready = asyncio.Event()

        async def runner(node, run):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            prompts[node["id"]] = node["prompt"]
            if node["id"] == "join":
                self.assertEqual(set(completed), {"a", "b"})
            try:
                if node["id"] in ("a", "b"):
                    if running == 2:
                        parallel_ready.set()
                    await asyncio.wait_for(parallel_ready.wait(), 15)
            finally:
                running -= 1
            completed.append(node["id"])
            return Result(output=json.dumps({"value": node["id"]}), telemetry={"total_tokens": 7})

        wf = workflow(
            [
                {"id": "brief", "type": "prompt", "prompt": "Authored {{params.topic}}"},
                agent("a"),
                agent("b"),
                agent(
                    "join",
                    prompt="{{nodes.a.output.value}}",
                    input_mapping={"second": "{{nodes.b.output}}"},
                ),
                {"id": "end", "type": "finish"},
            ],
            [
                {"from": "brief", "to": "a"},
                {"from": "brief", "to": "b"},
                {"from": "a", "to": "join"},
                {"from": "b", "to": "join"},
                {"from": "join", "to": "end"},
            ],
        )
        wf["params"] = [{"name": "topic", "type": "text", "label": "Topic"}]
        runtime = self.runtime(runner).start()
        state = runtime.engine.start(wf, input="Operator priority", params={"topic": "żółć"})
        # SQLite work can outlast the readiness budget on loaded Windows CI.
        # The barrier proves overlap; this deadline only bounds final settlement.
        result = self.wait(state["run_id"], timeout=30)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(peak, 2)
        self.assertIn("Authored żółć", prompts["join"])
        self.assertIn("Operator priority", prompts["a"])
        self.assertIn('"second"', prompts["join"])
        self.assertEqual(self.db.list_runs("all")[0]["total_tokens"], 21)
        self.assertEqual(self.db.list_runs("active"), [])

    def test_failure_branch_fallback_and_skip_join(self):
        visited = []

        async def runner(node, run):
            visited.append(node["id"])
            return Result("failure" if node["id"] == "check" else "success", output=node["id"])

        wf = workflow(
            [agent("check"), agent("pass"), agent("fix"), {"id": "end", "type": "finish"}],
            [
                {
                    "from": "check",
                    "to": "pass",
                    "condition": {"type": "node_status", "node": "check", "equals": "success"},
                },
                {"from": "check", "to": "fix", "fallback": True},
                {"from": "pass", "to": "end"},
                {"from": "fix", "to": "end"},
            ],
        )
        runtime = self.runtime(runner).start()
        state = runtime.engine.start(wf)
        result = self.wait(state["run_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["nodes"]["pass"]["status"], "skipped")
        self.assertEqual(visited, ["check", "fix"])

    def test_review_restart_note_and_duplicate_conflict(self):
        prompts = []

        async def runner(node, run):
            prompts.append(node["prompt"])
            return Result(output="done")

        wf = workflow(
            [
                {"id": "gate", "type": "human_review", "options": ["approved", "rejected"]},
                agent("work", prompt="Decision note: {{nodes.gate.review_note}}"),
                {"id": "end", "type": "finish"},
            ],
            [
                {
                    "from": "gate",
                    "to": "work",
                    "condition": {"type": "review_status", "equals": "approved"},
                },
                {"from": "work", "to": "end"},
            ],
        )
        self.specs.save({"workflow": wf}, create=True)
        runtime = self.runtime(runner).start()
        state = runtime.engine.start(wf)
        self.assertEqual(self.wait(state["run_id"])["status"], "waiting")
        runtime.close()
        restarted = self.runtime(runner).start()
        api = API(restarted)
        code, _ = api.handle(
            "POST",
            f"/runs/{state['run_id']}/review",
            {"node_id": "gate", "decision": "needs_changes"},
        )
        self.assertEqual(code, 400)
        payload = {"node_id": "gate", "decision": "approved", "note": "Use C:\\work\\żółć"}
        self.assertEqual(api.handle("POST", f"/runs/{state['run_id']}/review", payload)[0], 200)
        self.assertEqual(api.handle("POST", f"/runs/{state['run_id']}/review", payload)[0], 409)
        result = self.wait(state["run_id"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("C:\\work\\żółć", prompts[0])
        self.assertIn('"type": "review"', self.db.trace(state["run_id"]))

    def test_restart_interrupted_work_and_live_spec_retry(self):
        async def runner(node, run):
            return Result(output=node["prompt"])

        wf = workflow(
            [agent("work"), {"id": "end", "type": "finish"}], [{"from": "work", "to": "end"}]
        )
        self.specs.save({"workflow": wf})
        runtime = self.runtime(runner)
        state = runtime.engine.start(wf)
        self.db.mutate_run(
            state["run_id"],
            lambda r: (r.update(status="running"), r["nodes"]["work"].update(status="running")),
        )
        runtime.start()
        result = self.wait(state["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("interrupted", result["nodes"]["work"]["error"])
        wf["nodes"][0]["prompt"] = "updated fix"
        self.specs.save({"workflow": wf})
        API(runtime).handle("POST", f"/runs/{state['run_id']}/retry", {"node_id": "work"})
        result = self.wait(state["run_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["nodes"]["work"]["output"], "updated fix")

    def test_retry_after_recovery_commit_replaces_the_driver_spec(self):
        calls = []

        async def runner(node, run):
            calls.append(node["prompt"])
            return Result(output=node["prompt"])

        engine = Engine(self.db, Executor(agent_runner=runner))
        wf = workflow([agent("work")])
        state = engine.start(wf)
        run_id = state["run_id"]
        self.db.mutate_run(
            run_id,
            lambda r: (r.update(status="running"), r["nodes"]["work"].update(status="running")),
        )
        updated = copy.deepcopy(wf)
        updated["nodes"][0]["prompt"] = "updated fix"
        mutate = self.db.mutate_run
        retried = False

        def retry_after_recovery(*args, **kwargs):
            nonlocal retried
            snapshot = mutate(*args, **kwargs)
            if not retried and args[1].__name__ == "recover":
                retried = True
                self.assertEqual(snapshot["status"], "failed")
                engine.retry(run_id, updated, "work")
            return snapshot

        self.assertTrue(self.db.claim(run_id, "old-driver"))
        with patch.object(self.db, "mutate_run", side_effect=retry_after_recovery):
            asyncio.run(engine.drive(run_id, "old-driver"))
        self.assertTrue(retried)
        self.assertEqual(calls, [])
        self.assertEqual(self.db.get_run(run_id)["status"], "created")
        self.assertTrue(self.db.claim(run_id, "new-driver"))
        asyncio.run(engine.drive(run_id, "new-driver"))
        self.assertEqual(calls, ["updated fix"])
        result = self.db.get_run(run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["nodes"]["work"]["output"], "updated fix")

    def test_two_runtimes_execute_once_and_cancel(self):
        calls = []

        async def runner(node, run):
            calls.append(node["id"])
            await asyncio.sleep(10)
            return Result()

        first, second = self.runtime(runner).start(), self.runtime(runner).start()
        wf = workflow([agent("long")])
        state = first.engine.start(wf)
        self.wait(state["run_id"], ("running",))
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(calls), 1)
        second.engine.cancel(state["run_id"])
        self.assertEqual(self.wait(state["run_id"])["status"], "cancelled")
        time.sleep(0.2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.db.get_run(state["run_id"])["nodes"]["long"]["status"], "cancelled")

    def test_api_crud_errors_export_settings_and_schedules(self):
        runtime = self.runtime()
        api = API(runtime)
        wf = workflow()
        wf["trigger"] = {"type": "cron", "schedule": "*/5 * * * *", "timezone": "UTC"}
        payload = {"workflow": wf, "ui": {"nodes": {"finish": {"x": 100, "y": 200}}}}
        self.assertEqual(api.handle("POST", BASE + "/workflows", payload)[0], 200)
        self.assertEqual(api.handle("POST", "/workflows", payload)[0], 409)
        detail = api.handle("GET", "/workflows/demo")[1]
        self.assertEqual(detail["ui"], payload["ui"])
        self.assertTrue(Path(detail["path"]).is_file())
        self.assertEqual(api.handle("POST", "/workflows/demo/validate")[1]["valid"], True)
        preview = api.handle("POST", "/workflows/demo/compile-preview")[1]
        self.assertEqual(preview["first_node"], "finish")
        self.assertEqual(len(preview["cron_jobs"]), 1)
        exported = api.handle("GET", "/workflows/demo/export")[1]
        self.assertEqual(json.loads(exported["yaml"])["id"], "demo")
        self.assertIn("md", api.handle("GET", "/workflows/demo/export-template")[1])
        self.assertEqual(api.handle("PUT", "/workflows/demo/enabled", {"enabled": False})[0], 200)
        self.assertEqual(api.handle("POST", "/workflows/demo/run")[0], 409)
        self.assertEqual(api.handle("PUT", "/workflows/demo/enabled", {"enabled": True})[0], 200)
        self.assertEqual(api.handle("PUT", "/workflows/demo", payload)[0], 200)
        schedule = api.handle("GET", "/schedules")[1]["schedules"][0]
        route = "/schedules/" + schedule["hermes_cron_id"]
        self.assertEqual(api.handle("POST", route + "/pause")[0], 200)
        self.assertFalse(api.handle("GET", "/schedules")[1]["schedules"][0]["enabled"])
        self.assertEqual(api.handle("POST", route + "/resume")[0], 200)
        self.assertEqual(api.handle("PUT", route, {"cron": "99 * * * *"})[0], 400)
        self.assertEqual(api.handle("PUT", route, {"cron": "0 * * * *"})[0], 200)
        self.assertEqual(api.handle("PUT", "/settings", {"trace_enabled": True})[0], 200)
        self.assertEqual(api.handle("PUT", "/settings", {"default_timeout_seconds": True})[0], 400)
        self.assertEqual(api.handle("PUT", "/settings", {"unknown": 1})[0], 400)
        self.assertIn("schema", api.handle("GET", "/settings")[1])
        self.assertEqual(api.handle("POST", route + "/run")[0], 200)
        run_id = self.db.list_runs("all")[0]["run_id"]
        self.assertEqual(self.wait(run_id)["status"], "completed")
        exported_run = api.handle("GET", f"/runs/{run_id}/export")[1]
        self.assertEqual(exported_run["json"]["run_id"], run_id)
        self.assertIn("trace", exported_run)
        self.assertEqual(len(api.handle("GET", "/runs?scope=all&workflow_id=demo")[1]["runs"]), 1)
        self.assertEqual(api.handle("GET", "/runs?scope=all&workflow_id=other")[1]["runs"], [])
        self.assertEqual(
            api.handle("GET", "/workflows")[1]["workflows"][0]["last_status"], "completed"
        )
        self.assertEqual(api.handle("GET", "/runs/missing")[0], 404)
        self.assertEqual(api.handle("GET", "/workflows/missing")[0], 404)
        self.assertEqual(api.handle("GET", "/runs?scope=bad")[0], 400)
        self.assertEqual(api.handle("PATCH", "/settings")[0], 405)
        self.assertEqual(api.handle("GET", "/o2b-status")[0], 200)
        self.assertEqual(api.handle("GET", "/profiles")[0], 200)
        self.assertEqual(api.handle("POST", "/workflows", [1])[0], 400)
        self.assertEqual(api.handle("DELETE", route)[0], 200)
        runtime.sync_schedule(wf)
        self.assertEqual(api.handle("GET", "/schedules")[1]["schedules"], [])
        self.assertEqual(api.handle("DELETE", "/workflows/demo")[1], {"deleted": True})

    def test_review_controls_use_the_run_snapshot(self):
        runtime = self.runtime()
        api = API(runtime)
        wf = workflow([{"id": "gate", "type": "human_review", "options": ["approved"]}])
        self.specs.save({"workflow": wf})
        state = runtime.engine.start(wf)
        wf["nodes"][0]["options"] = ["rejected"]
        self.specs.save({"workflow": wf})
        status, detail = api.handle("GET", f"/runs/{state['run_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["nodes"]["gate"]["review_options"], ["approved"])
        self.assertNotIn("review_options", self.db.get_run(state["run_id"])["nodes"]["gate"])

    def test_cron_due_time_claimed_once(self):
        runtime = self.runtime()
        wf = workflow()
        wf["trigger"] = {"type": "cron", "schedule": "* * * * *"}
        self.specs.save({"workflow": wf})
        runtime.sync_schedule(wf)
        row = self.db.schedules()[0]
        row["next_run"] = "2000-01-01T00:00:00+00:00"
        self.db.put_schedule(row)
        second = self.runtime()
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda r: r.tick_schedules(), (runtime, second)))
        self.assertEqual(len(self.db.list_runs("all")), 1)

    @unittest.skipUnless(os.name == "nt", "Windows shells")
    def test_native_cmd_powershell_and_unicode_workdir(self):
        runtime = self.runtime().start()
        for shell, command in (
            ("cmd", 'echo hello> "result.txt"'),
            ("powershell", "'żółć' | Set-Content -Encoding UTF8 result.txt; Write-Output 'done'"),
        ):
            workdir = self.root / (shell + " space żółć")
            workdir.mkdir()
            wf = workflow(
                [
                    {
                        "id": "script",
                        "type": "script",
                        "shell": shell,
                        "command": command,
                        "workdir": str(workdir),
                        "timeout_seconds": 60,
                    }
                ],
                id=shell,
            )
            state = runtime.engine.start(wf)
            # Clean hosted Windows images can pay a substantial first-start
            # PowerShell/.NET cost. Keep production deadlines unchanged while
            # allowing this actual-shell integration check to finish.
            result = self.wait(state["run_id"], timeout=65)
            self.assertEqual(result["status"], "completed", result)
            print(f"Native {shell}: {result['nodes']['script']['telemetry']['duration_ms']} ms")
            text = (workdir / "result.txt").read_text(encoding="utf-8-sig").strip()
            self.assertEqual(text, "hello" if shell == "cmd" else "żółć")

    def test_real_subprocess_timeout_cleans_grandchild(self):
        marker = self.root / "child survived.txt"
        child = self.root / "child.py"
        child.write_text(
            "import time\nfrom pathlib import Path\ntime.sleep(1)\nPath("
            + repr(str(marker))
            + ").write_text('survived')\n",
            encoding="utf-8",
        )
        parent = self.root / "parent.py"
        parent.write_text(
            "import subprocess,sys,time\nsubprocess.Popen([sys.executable,"
            + repr(str(child))
            + "])\ntime.sleep(20)\n",
            encoding="utf-8",
        )
        with self.assertRaises(TimeoutError):
            asyncio.run(run_process([sys.executable, str(parent)], timeout=0.3))
        time.sleep(1.1)
        self.assertFalse(marker.exists(), "grandchild survived timeout")

    @unittest.skipUnless(os.name == "nt", "Windows cancellation")
    def test_engine_cancel_kills_real_process_tree(self):
        started = self.root / "parent started.txt"
        marker = self.root / "cancel child survived.txt"
        child = self.root / "cancel child.py"
        child.write_text(
            "import time\nfrom pathlib import Path\ntime.sleep(1)\nPath("
            + repr(str(marker))
            + ").write_text('survived')\n",
            encoding="utf-8",
        )
        parent = self.root / "cancel parent.py"
        parent.write_text(
            "import subprocess,sys,time\nfrom pathlib import Path\nsubprocess.Popen([sys.executable,"
            + repr(str(child))
            + "])\nPath("
            + repr(str(started))
            + ").write_text('started')\ntime.sleep(20)\n",
            encoding="utf-8",
        )
        wf = workflow(
            [
                {
                    "id": "work",
                    "type": "script",
                    "shell": "powershell",
                    "command": f'& "{sys.executable}" "{parent}"',
                }
            ]
        )
        runtime = self.runtime().start()
        state = runtime.engine.start(wf)
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(started.exists(), self.db.get_run(state["run_id"]))
        runtime.engine.cancel(state["run_id"])
        time.sleep(1.2)
        self.assertFalse(marker.exists())
        self.assertEqual(self.db.get_run(state["run_id"])["status"], "cancelled")

    @unittest.skipUnless(os.name == "nt", "Windows env and encoding")
    def test_script_env_allowlist_unicode_output_and_exit_codes(self):
        runtime = self.runtime().start()
        save(self.db, {"script_env_allowlist": "HWP_PUBLIC"})
        with patch.dict(os.environ, {"HWP_PUBLIC": "allowed", "HWP_SECRET": "blocked"}):
            wf = workflow(
                [
                    {
                        "id": "work",
                        "type": "script",
                        "shell": "powershell",
                        "command": "Write-Output ('żółć|' + $env:HWP_PUBLIC + '|' + $env:HWP_SECRET)",
                        "timeout_seconds": 60,
                    }
                ]
            )
            state = runtime.engine.start(wf)
            result = self.wait(state["run_id"], timeout=65)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["nodes"]["work"]["output"].strip(), "żółć|allowed|")
        wf["nodes"][0]["command"] = "exit 7"
        state = runtime.engine.start(wf)
        result = self.wait(state["run_id"], timeout=65)
        self.assertEqual(result["status"], "failed")
        self.assertIn("code 7", result["nodes"]["work"]["error"])

    @unittest.skipUnless(os.name == "nt", "Windows smoke example")
    def test_cli_review_across_processes_in_isolated_home(self):
        home = self.root / "isolated hermes"
        db = Database(home / "workflows" / "runs.db")
        save(db, {"scripts_enabled": True})
        env = dict(
            os.environ, HERMES_HOME=str(home), PYTHONPATH=str(PLUGIN), PYTHONIOENCODING="utf-8"
        )

        def cli(*arguments):
            result = subprocess.run(
                [sys.executable, "-m", "hermes_workflows", *arguments],
                cwd=PLUGIN,
                env=env,
                capture_output=True,
                encoding="utf-8",
                timeout=75,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)

        # This tests process lifetime/review persistence, not a cold shell's
        # startup speed. Adapt only the isolated fixture's execution budget.
        smoke = json.loads((PLUGIN / "examples/windows-smoke.workflow.json").read_text("utf-8"))
        smoke["nodes"][0]["timeout_seconds"] = 60
        smoke_path = self.root / "hosted-smoke.workflow.json"
        smoke_path.write_text(json.dumps(smoke), encoding="utf-8")
        cli("import", str(smoke_path))
        waiting = cli("run", "windows-smoke")
        self.assertEqual(waiting["status"], "waiting")
        done = cli(
            "review", waiting["run_id"], "review", "approved", "--note", "CLI restart passed"
        )
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["nodes"]["review"]["review_note"], "CLI restart passed")

    def test_installer_copy_and_refuse_overwrite(self):
        spec = importlib.util.spec_from_file_location("workflow_installer", PLUGIN / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = self.root / "installed plugin"
        # Windows TEMP may use an 8.3 spelling, while install() resolves it.
        self.assertEqual(module.install(target), target.resolve())
        desktop = target / "desktop" / "plugin.js"
        self.assertTrue(desktop.is_file())
        self.assertEqual(desktop.read_bytes(), (PLUGIN / "desktop" / "plugin.js").read_bytes())
        self.assertTrue((target / "plugin.yaml").is_file())
        self.assertTrue((target / "dashboard" / "plugin_api.py").is_file())
        self.assertTrue((target / "_bootstrap.py").is_file())
        loader_spec = importlib.util.spec_from_file_location(
            "installed_workflow_loader", target / "_bootstrap.py"
        )
        loader = importlib.util.module_from_spec(loader_spec)
        loader_spec.loader.exec_module(loader)
        backend = loader.load_backend(target)
        self.assertEqual(
            Path(backend.__file__).resolve(), target.resolve() / "hermes_workflows" / "__init__.py"
        )
        for name in tuple(sys.modules):
            if name == backend.__name__ or name.startswith(backend.__name__ + "."):
                sys.modules.pop(name, None)
        with self.assertRaises(FileExistsError):
            module.install(target)
        module.install(target, update=True)

    def test_http_and_asgi_end_to_end(self):
        from hermes_workflows.server import make_server

        runtime = self.runtime().start()
        api = API(runtime)
        server = make_server("127.0.0.1", 0, api=api, token="test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + BASE + "/workflows")
            self.assertEqual(error.exception.code, 401)
            request = urllib.request.Request(
                url + BASE + "/workflows",
                data=json.dumps({"workflow": workflow()}).encode(),
                headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["workflow"]["id"], "demo")
            request = urllib.request.Request(
                url + "/dashboard/dist/index.js", headers={"Authorization": "Bearer test-token"}
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(
                    hashlib.sha256(response.read()).hexdigest(),
                    json.loads((PLUGIN / "dashboard/upstream.json").read_text())["files"][
                        "dist/index.js"
                    ],
                )
            request = urllib.request.Request(
                url + BASE + "/workflows/demo/run",
                data=b"{",
                headers={"Authorization": "Bearer test-token"},
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

        async def asgi_request(body, path="/workflows", method="GET"):
            sent = []

            async def receive():
                return {"type": "http.request", "body": body}

            async def send(message):
                sent.append(message)

            await ASGIApp(api)(
                {"type": "http", "path": BASE + path, "method": method}, receive, send
            )
            return sent

        sent = asyncio.run(asgi_request(b""))
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(json.loads(sent[1]["body"])["workflows"][0]["id"], "demo")
        self.assertEqual(asyncio.run(asgi_request(b"{"))[0]["status"], 400)

    @unittest.skipUnless(
        importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"),
        "optional host dependencies",
    )
    def test_real_fastapi_router_mount_and_yaml_import(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_workflows.api import create_router

        runtime = self.runtime().start()
        application = FastAPI()
        application.include_router(create_router(API(runtime)), prefix=BASE)
        with TestClient(application) as client:
            self.assertEqual(
                client.post(BASE + "/workflows", json={"workflow": workflow()}).status_code, 200
            )
            response = client.post(BASE + "/workflows/demo/run", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.wait(response.json()["run_id"])
            self.assertEqual(
                len(client.get(BASE + "/runs?scope=all&workflow_id=demo").json()["runs"]), 1
            )
            self.assertEqual(
                client.get(BASE + "/runs?scope=all&workflow_id=other").json()["runs"], []
            )
            self.assertEqual(
                client.post(
                    BASE + "/workflows/demo/run",
                    content="{",
                    headers={"Content-Type": "application/json"},
                ).status_code,
                400,
            )
        yaml_path = self.root / "global" / "yaml.workflow.yaml"
        yaml_path.write_text(
            "id: yaml\nname: YAML workflow\nversion: 1\nscope: {type: global}\ntrigger: {type: manual}\nnodes:\n  - {id: end, type: finish}\nedges: []\nui: {nodes: {end: {x: 10, y: 20}}}\n",
            encoding="utf-8",
        )
        detail = self.specs.get("yaml")
        self.assertEqual(detail["ui"]["nodes"]["end"]["x"], 10)
        self.specs.save(detail)
        self.assertEqual(self.specs.get("yaml")["workflow"]["id"], "yaml")

    def test_isolated_python_agent_worker_profile_and_model(self):
        # Exercise the actual child-Python/file-result path with a fake Hermes
        # installation; no provider requests or user credentials are involved.
        host = self.root / "host checkout"
        (host / "hermes_cli").mkdir(parents=True)
        (host / "tools").mkdir()
        for package in ("hermes_cli", "tools"):
            (host / package / "__init__.py").write_text("", encoding="utf-8")
        (host / "hermes_cli" / "profiles.py").write_text(
            "def resolve_profile_env(name):\n return 'C:/fake-profile/' + name\n", encoding="utf-8"
        )
        (host / "hermes_cli" / "config.py").write_text(
            "def load_config():\n return {'model': {'default':'fallback'}}\n", encoding="utf-8"
        )
        (host / "hermes_cli" / "runtime_provider.py").write_text(
            "def resolve_runtime_provider(**kwargs):\n return {'provider': kwargs['requested'], 'api_key': 'fake'}\n",
            encoding="utf-8",
        )
        (host / "hermes_cli" / "env_loader.py").write_text(
            "import os\ndef load_hermes_dotenv(**kwargs):\n os.environ['WORKFLOW_TEST_DOTENV_HOME']=kwargs['hermes_home']\n",
            encoding="utf-8",
        )
        (host / "tools" / "skills_tool.py").write_text(
            "def skill_view(name):\n return 'skill ' + name\n", encoding="utf-8"
        )
        (host / "run_agent.py").write_text(
            "import json,os\nclass AIAgent:\n def __init__(self, **kwargs):\n  self.kwargs=kwargs\n def run_conversation(self,prompt):\n  print('logging should not corrupt output')\n  return {'final_response': json.dumps({'kwargs':self.kwargs,'prompt':prompt,'home':os.environ['HERMES_HOME'],'dotenv_home':os.environ.get('WORKFLOW_TEST_DOTENV_HOME'),'worker_flag':os.environ.get('HERMES_WORKFLOWS_WORKER')}), 'total_tokens':12}\n def close(self):\n  pass\n",
            encoding="utf-8",
        )
        wf = workflow(
            [agent("work", profile="native", model="test-model@test-provider", skills=["coding"])]
        )
        original_home = os.environ.get("HERMES_HOME")
        # Only the child needs this partial Hermes fixture. Exposing it to the
        # parent makes native config discovery depend on earlier module imports.
        with patch.dict(os.environ, {"PYTHONPATH": str(host)}):
            runtime = self.runtime().start()
            state = runtime.engine.start(wf)
            result = self.wait(state["run_id"])
        self.assertEqual(result["status"], "completed", result)
        output = json.loads(result["nodes"]["work"]["output"])
        self.assertEqual(output["kwargs"]["provider"], "test-provider")
        self.assertEqual(output["kwargs"]["model"], "test-model")
        self.assertEqual(output["home"], "C:/fake-profile/native")
        self.assertEqual(output["dotenv_home"], output["home"])
        self.assertEqual(output["worker_flag"], "1")
        self.assertIn("skill coding", output["prompt"])
        self.assertEqual(os.environ.get("HERMES_HOME"), original_home)
        self.assertEqual(result["nodes"]["work"]["telemetry"]["total_tokens"], 12)

    def test_wait_timeout_and_unhandled_review(self):
        async def wait_runner(node, run):
            await asyncio.sleep(10)
            return Result()

        runtime = Runtime(self.db, self.specs, Executor(wait_runner=wait_runner))
        self.runtimes.append(runtime)
        runtime.start()
        wf = workflow(
            [
                {
                    "id": "wait",
                    "type": "wait",
                    "wait_for": {"github_pr_merged": "https://github.com/owner/repo/pull/1"},
                    "timeout_seconds": 0.1,
                }
            ]
        )
        state = runtime.engine.start(wf)
        result = self.wait(state["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("TimeoutError", result["nodes"]["wait"]["error"])
        wf = workflow(
            [{"id": "gate", "type": "human_review"}, {"id": "end", "type": "finish"}],
            [
                {
                    "from": "gate",
                    "to": "end",
                    "condition": {"type": "review_status", "equals": "approved"},
                }
            ],
            id="review-reject",
        )
        state = runtime.engine.start(wf)
        self.wait(state["run_id"], ("waiting",))
        runtime.engine.review(state["run_id"], "gate", "rejected")
        result = self.wait(state["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["nodes"]["end"]["status"], "skipped")

    def test_agent_timeout_reports_last_worker_stage(self):
        host = self.root / "slow host"
        (host / "hermes_cli").mkdir(parents=True)
        (host / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (host / "hermes_cli" / "profiles.py").write_text(
            "def resolve_profile_env(name):\n return 'C:/fake-profile'\n", encoding="utf-8"
        )
        (host / "hermes_cli" / "env_loader.py").write_text(
            "import time\ndef load_hermes_dotenv(**kwargs):\n time.sleep(10)\n", encoding="utf-8"
        )
        runtime = self.runtime().start()
        with patch.dict(os.environ, {"PYTHONPATH": str(host)}):
            # Allow a cold Windows interpreter to reach the deliberately
            # stalled stage; 0.7s also timed out normal process startup.
            state = runtime.engine.start(workflow([agent("slow", timeout_seconds=3)]))
            result = self.wait(state["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("loading_profile_environment", result["nodes"]["slow"]["error"])


class PureTests(unittest.TestCase):
    def test_validation_rejects_cycle_bad_ref_path_traversal_and_types(self):
        valid = workflow(
            [agent("a"), {"id": "end", "type": "finish"}], [{"from": "a", "to": "end"}]
        )
        self.assertTrue(validate(valid)["valid"])
        cases = []
        bad = copy.deepcopy(valid)
        bad["edges"].append({"from": "end", "to": "a"})
        cases.append(bad)
        bad = copy.deepcopy(valid)
        bad["nodes"][0]["prompt"] = "{{nodes.end.output}}"
        cases.append(bad)
        bad = copy.deepcopy(valid)
        bad["id"] = "../escape"
        cases.append(bad)
        bad = copy.deepcopy(valid)
        bad["nodes"][0]["timeout_seconds"] = -1
        cases.append(bad)
        bad = copy.deepcopy(valid)
        bad["nodes"][0]["adopt"] = True
        cases.append(bad)
        for bad in cases:
            self.assertFalse(validate(bad)["valid"], bad)

    def test_param_types_and_unknowns(self):
        wf = workflow()
        wf["params"] = [
            {"name": "count", "type": "int"},
            {"name": "on", "type": "bool", "default": False},
        ]
        self.assertEqual(fill_params(wf, {"count": "3", "on": "true"}), {"count": 3, "on": True})
        for params in ({"count": "1.5"}, {"count": True}, {"oops": "x"}):
            with self.assertRaises(ValueError):
                fill_params(wf, params)
        with self.assertRaises(ValueError):
            fill_params(wf, {})
        wf["params"][0]["optional"] = True
        self.assertEqual(fill_params(wf, {}), {"on": False})

    def test_cron_bounds_or_semantics_and_leap_year(self):
        cron = Cron("*/15 10 * * 1-5")
        self.assertEqual(cron.next(datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)).minute, 15)
        self.assertEqual(
            Cron("0 0 29 2 *").next(datetime(2026, 1, 1, tzinfo=timezone.utc)).year, 2028
        )
        self.assertTrue(Cron("0 0 1 * 1").matches(datetime(2026, 11, 2, tzinfo=timezone.utc)))
        for value in ("* * * *", "99 * * * *", "*/0 * * * *", "0 24 * * *", "0 0 0 * *"):
            with self.assertRaises(ValueError):
                Cron(value)

    def test_cron_keeps_first_minute_of_allowed_month(self):
        after = datetime(2026, 10, 31, 5, 52, tzinfo=timezone.utc)
        self.assertEqual(
            Cron("0 0 1 1 *").next(after),
            datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        # A month can begin between UTC hour boundaries. No tzdata dependency
        # is required to exercise a real half-hour offset.
        from datetime import timedelta

        cron = Cron("0 0 1 1 *")
        cron.zone = timezone(timedelta(hours=5, minutes=30))
        self.assertEqual(cron.next(after), datetime(2026, 12, 31, 18, 30, tzinfo=timezone.utc))

    def test_plugin_registration_and_assets_unchanged(self):
        class Context:
            def __init__(self):
                self.tools, self.commands = [], []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

            def register_command(self, *args, **kwargs):
                self.commands.append((args, kwargs))

        context = Context()
        with patch("hermes_workflows.runtime.get_runtime"):
            register(context)
        self.assertEqual(
            {t["name"] for t in context.tools},
            {
                "workflow_list",
                "workflow_run",
                "workflow_status",
                "workflow_explain",
                "workflow_review",
                "workflow_resume",
            },
        )
        self.assertEqual(context.commands[0][0][0], "workflow")
        spec = importlib.util.spec_from_file_location(
            "hermes_workflows_plugin_entry", PLUGIN / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.register, register)
        source = json.loads((PLUGIN / "dashboard/upstream.json").read_text())
        for path, expected in {**source["files"], **source.get("local_files", {})}.items():
            self.assertEqual(
                hashlib.sha256((PLUGIN / "dashboard" / path).read_bytes()).hexdigest(),
                expected,
                path,
            )
        manifest = json.loads((PLUGIN / "dashboard/manifest.json").read_text())
        self.assertEqual(manifest["entry"], "entry.js")
        self.assertEqual(manifest["css"], "style.css")
        self.assertIn(("POST", "/runs/{run_id}/review"), ROUTES)


if __name__ == "__main__":
    unittest.main()
