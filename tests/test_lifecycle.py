from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))

from hermes_workflows import register
from hermes_workflows.db import Database
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Executor, Result
from hermes_workflows.runtime import Runtime, get_runtime, own_runtime, stop_runtime
from hermes_workflows.specs import SpecStore


def workflow(name, nodes=None):
    return {
        "id": name,
        "name": name,
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "nodes": nodes or [{"id": "finish", "type": "finish"}],
        "edges": [],
    }


class Context:
    def __init__(self):
        self.tools, self.commands, self.cleanups = {}, {}, []

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs["handler"]

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def on_unload(self, callback):
        self.cleanups.append(callback)

    def unload(self):
        for callback in reversed(self.cleanups):
            callback()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow lifecycle ")
        self.home = Path(self.temp.name)
        self.home_patch = patch("hermes_workflows.db.hermes_home", return_value=self.home)
        self.home_patch.start()
        self.db = Database(self.home / "workflows" / "runs.db")
        self.store = SpecStore([self.home / "workflows" / "global"])
        self.contexts = []
        self.runtimes = []

    def tearDown(self):
        for ctx in self.contexts:
            ctx.unload()
        for runtime in self.runtimes:
            stop_runtime(runtime)
        self.home_patch.stop()
        self.temp.cleanup()

    def context(self):
        ctx = Context()
        self.contexts.append(ctx)
        register(ctx)
        return ctx

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail("lifecycle condition did not become true")

    def test_registration_recovers_and_fires_due_cron_without_tool_call(self):
        interrupted = workflow(
            "interrupted",
            [{"id": "work", "type": "agent_task", "profile": "default", "prompt": "unused"}],
        )
        state = Engine(self.db).start(interrupted)
        self.db.mutate_run(
            state["run_id"],
            lambda r: (r.update(status="running"), r["nodes"]["work"].update(status="running")),
        )
        scheduled = workflow("scheduled")
        scheduled["trigger"] = {"type": "cron", "schedule": "* * * * *", "timezone": "UTC"}
        self.store.save({"workflow": scheduled})
        bootstrap = Runtime(self.db, self.store)
        bootstrap.sync_schedule(scheduled)
        row = self.db.schedules()[0]
        row["next_run"] = "2000-01-01T00:00:00+00:00"
        self.db.put_schedule(row)
        self.context()
        self.wait_for(lambda: self.db.get_run(state["run_id"])["status"] == "failed")
        rows = self.wait_for(
            lambda: [r for r in self.db.list_runs("all", "scheduled") if r["status"] == "completed"]
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("interrupted", self.db.get_run(state["run_id"])["nodes"]["work"]["error"])

    def test_cold_boot_discovers_cron_specs(self):
        scheduled = workflow("new-schedule")
        scheduled["trigger"] = {"type": "cron", "schedule": "*/5 * * * *"}
        self.store.save({"workflow": scheduled})
        self.context()
        self.assertEqual(self.db.schedules()[0]["workflow_id"], "new-schedule")

    def test_unload_cancels_active_work_and_releases_lease(self):
        started, cancelled = threading.Event(), threading.Event()

        async def runner(node, run):
            started.set()
            try:
                await asyncio.sleep(60)
                return Result()
            finally:
                cancelled.set()

        original_factory = Runtime

        def factory(db, specs):
            return original_factory(db, specs, Executor(agent_runner=runner))

        with patch("hermes_workflows.runtime.Runtime", side_effect=factory):
            ctx = self.context()
        runtime = get_runtime(self.home)
        state = runtime.engine.start(
            workflow(
                "active",
                [{"id": "work", "type": "agent_task", "profile": "default", "prompt": "unused"}],
            )
        )
        self.assertTrue(started.wait(5))
        ctx.unload()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(runtime.thread.is_alive())
        with self.db.connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT lease_until FROM py_runs WHERE run_id=?", (state["run_id"],)
                ).fetchone()[0],
                0,
            )

    def test_reload_creates_new_runtime_and_old_cleanup_cannot_stop_it(self):
        first = self.context()
        old = get_runtime(self.home)
        first.unload()
        self.context()
        current = get_runtime(self.home)
        self.assertIsNot(current, old)
        first.unload()
        self.assertFalse(current.closed)
        self.assertTrue(current.thread.is_alive())

    def test_handlers_keep_registration_home_across_profile_switches(self):
        self.store.save({"workflow": workflow("first-home")})
        first = self.context()
        second_home = self.home / "another-profile"
        second_runtime = get_runtime(second_home)
        self.runtimes.append(second_runtime)
        second_runtime.specs.save({"workflow": workflow("second-home")})
        with patch("hermes_workflows.db.hermes_home", return_value=second_home):
            result = json.loads(first.tools["workflow_list"]({}))
            command = json.loads(first.commands["workflow"]("list"))
        self.assertEqual([w["id"] for w in result["workflows"]], ["first-home"])
        self.assertEqual(command, result)

    def test_runtime_thread_inherits_host_context(self):
        scope = contextvars.ContextVar("test-profile", default="wrong")
        seen = []
        runtime = Runtime(self.db, self.store)
        original_tick = runtime.tick_schedules

        def tick():
            seen.append(scope.get())
            original_tick()

        runtime.tick_schedules = tick
        self.runtimes.append(runtime)
        token = scope.set("correct")
        try:
            runtime.start()
        finally:
            scope.reset(token)
        self.wait_for(lambda: seen)
        self.assertEqual(seen[0], "correct")

    def test_agent_worker_registration_does_not_start_supervisor(self):
        with (
            patch.dict(os.environ, {"HERMES_WORKFLOWS_WORKER": "1"}),
            patch("hermes_workflows.runtime.get_runtime") as start,
        ):
            register(Context())
        start.assert_not_called()

    def test_closed_runtime_cannot_be_restarted(self):
        runtime = Runtime(self.db, self.store)
        runtime.start()
        runtime.close()
        runtime.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            runtime.start()

    def test_shared_owner_release_is_idempotent(self):
        runtime = get_runtime(self.home)
        self.runtimes.append(runtime)
        plugin_release = own_runtime(runtime)
        http_release = own_runtime(runtime)
        http_release()
        http_release()
        self.assertFalse(runtime.closed)
        plugin_release()
        self.assertTrue(runtime.closed)

    @unittest.skipUnless(
        importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"),
        "optional host dependencies",
    )
    def test_http_shutdown_keeps_plugin_owned_runtime(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_workflows.api import API, create_router

        ctx = self.context()
        runtime = get_runtime(self.home)
        application = FastAPI()
        application.include_router(
            create_router(API(runtime)), prefix="/api/plugins/hermes-workflows"
        )
        with TestClient(application) as client:
            self.assertEqual(client.get("/api/plugins/hermes-workflows/runs").status_code, 200)
        self.assertFalse(runtime.closed)
        ctx.unload()
        self.assertTrue(runtime.closed)


if __name__ == "__main__":
    unittest.main()
