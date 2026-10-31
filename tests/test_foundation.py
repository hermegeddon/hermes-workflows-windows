from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-workflows-py"
sys.path.insert(0, str(PLUGIN))
from hermes_workflows.db import ConflictError, Database
from hermes_workflows.effects import EffectStore
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Executor, Result
from hermes_workflows.migrations import V1, statements
from hermes_workflows.native import NativeHost, NativeUnavailable
from hermes_workflows.runtime import Runtime
from hermes_workflows.specs import SpecStore


def workflow(prompt="old"):
    return {
        "id": "foundation",
        "name": "Foundation",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "default"},
        "nodes": [{"id": "work", "type": "agent_task", "prompt": prompt}],
        "edges": [],
    }


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow foundation żółć ")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runs.db"

    def legacy(self):
        state = {
            "run_id": "legacy",
            "workflow_id": "legacy",
            "status": "completed",
            "started_at": 1,
            "finished_at": 2,
            "nodes": {
                "done": {
                    "node_id": "done",
                    "node_type": "finish",
                    "status": "completed",
                    "outcome": "success",
                }
            },
        }
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("CREATE TABLE py_metadata(version INTEGER NOT NULL)")
            con.execute("INSERT INTO py_metadata VALUES(1)")
            statements(con, V1)
            con.execute(
                "INSERT INTO py_runs VALUES(?,?,?,?,?,?,?,?)",
                (
                    "legacy",
                    "legacy",
                    "completed",
                    json.dumps(state),
                    '{"original": true}',
                    1,
                    None,
                    0,
                ),
            )
            con.execute("INSERT INTO py_events VALUES(1,'legacy',1,'{\"type\":\"original\"}')")
            con.execute("INSERT INTO py_settings VALUES('scripts_enabled','true')")
            con.execute("INSERT INTO py_schedules VALUES('job','legacy','{\"enabled\":false}')")
            con.execute("INSERT INTO py_exports VALUES('cached','{\"original\":true}')")
        return state

    def test_v1_migration_preserves_run_spec_events_and_other_stores(self):
        state = self.legacy()
        db = Database(self.path)
        self.assertEqual(db.get_run("legacy"), state)
        self.assertEqual(db.get_spec("legacy"), {"original": True})
        self.assertEqual(db.settings(), {"scripts_enabled": True})
        self.assertEqual(db.schedule("job"), {"enabled": False})
        self.assertEqual(db.export_cache("cached"), {"original": True})
        self.assertIn('"type": "original"', db.trace("legacy"))
        self.assertEqual(db.attempts("legacy")[0]["data"], state["nodes"]["done"])
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT version FROM py_metadata").fetchone()[0], 2)
        Database(self.path)
        self.assertEqual(len(db.attempts("legacy")), 1)

    def test_failed_migration_rolls_back_ddl_and_version(self):
        state = self.legacy()

        def interrupted(con, sql):
            con.execute("ALTER TABLE py_runs ADD COLUMN revision INTEGER DEFAULT 0")
            raise RuntimeError("simulated crash")

        with patch("hermes_workflows.migrations.statements", interrupted):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                Database(self.path)
        with closing(sqlite3.connect(self.path)) as con, con:
            self.assertEqual(con.execute("SELECT version FROM py_metadata").fetchone()[0], 1)
            self.assertNotIn("revision", {r[1] for r in con.execute("PRAGMA table_info(py_runs)")})
        self.assertEqual(Database(self.path).get_run("legacy"), state)

    def test_newer_schema_is_rejected_without_modifying_data(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE py_metadata SET version=999")
        with self.assertRaisesRegex(ValueError, "unsupported workflow database version"):
            Database(self.path)
        with closing(sqlite3.connect(self.path)) as con, con:
            self.assertEqual(con.execute("SELECT version FROM py_metadata").fetchone()[0], 999)
            self.assertNotIn("revision", {r[1] for r in con.execute("PRAGMA table_info(py_runs)")})

    def test_concurrent_process_migration_is_idempotent(self):
        self.legacy()
        code = "from hermes_workflows.db import Database; import sys; Database(sys.argv[1])"
        env = {**os.environ, "PYTHONPATH": str(PLUGIN)}
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(self.path)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, (out, err))
        self.assertEqual(len(Database(self.path).attempts("legacy")), 1)

    def active(self):
        db = Database(self.path)
        engine = Engine(db)
        run_id = engine.start(workflow())["run_id"]
        self.assertTrue(db.claim(run_id, "driver"))
        run = db.mutate_run(
            run_id,
            lambda r: (r.update(status="running"), r["nodes"]["work"].update(status="running")),
            owner="driver",
            revision=0,
        )
        return db, engine, run_id, run["nodes"]["work"]["attempt_id"]

    def test_retry_revokes_lease_and_preserves_attempt_handle_history(self):
        db, engine, run_id, attempt_id = self.active()
        db.attach_attempt_handle(
            run_id, "work", attempt_id, {"card": "native-card"}, owner="driver", revision=0
        )
        engine.cancel(run_id)
        engine.retry(run_id, workflow("new"))
        with self.assertRaises(ConflictError):
            db.mutate_run(
                run_id, lambda r: r.update(status="completed"), owner="driver", revision=0
            )
        self.assertFalse(db.claim(run_id, "driver", revision=0))
        self.assertTrue(db.claim(run_id, "replacement", revision=2))
        db.release(run_id, "driver")
        run = db.mutate_run(
            run_id,
            lambda r: r["nodes"]["work"].update(status="running"),
            owner="replacement",
            revision=2,
        )
        new_id = run["nodes"]["work"]["attempt_id"]
        self.assertNotEqual(attempt_id, new_id)
        self.assertEqual(run["nodes"]["work"]["attempt"], 2)
        history = db.attempts(run_id)
        self.assertEqual(history[0]["status"], "cancelled")
        self.assertEqual(history[0]["handle"], {"card": "native-card"})
        with self.assertRaises(ConflictError):
            db.mutate_run(
                run_id,
                lambda r: None,
                owner="replacement",
                revision=2,
                attempt=("work", attempt_id),
            )

    def test_cancellation_ignoring_worker_cannot_settle_a_retry(self):
        started = threading.Event()

        async def runner(node, run):
            if node["prompt"] == "old":
                started.set()
                try:
                    await asyncio.sleep(100)
                except asyncio.CancelledError:
                    return Result(output="obsolete result")
            return Result(output="new result")

        db = Database(self.path)
        runtime = Runtime(
            db, SpecStore([Path(self.temp.name) / "global"]), Executor(agent_runner=runner)
        )
        self.addCleanup(runtime.close)
        runtime.start()
        run_id = runtime.engine.start(workflow())["run_id"]
        self.assertTrue(started.wait(5))
        runtime.engine.cancel(run_id)
        runtime.engine.retry(run_id, workflow("new"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = db.get_run(run_id)
            if run["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["nodes"]["work"]["output"], "new result")
        self.assertEqual([r["status"] for r in db.attempts(run_id)], ["cancelled", "completed"])

    def test_transient_retries_get_separate_attempts(self):
        calls = []

        async def runner(node, run):
            calls.append(run["nodes"]["work"]["attempt_id"])
            return Result("failure", error="503 busy") if len(calls) == 1 else Result(output="ok")

        db = Database(self.path)
        engine = Engine(db, Executor(agent_runner=runner))
        wf = workflow()
        wf["nodes"][0]["max_retries"] = 1
        run_id = engine.start(wf)["run_id"]
        self.assertTrue(db.claim(run_id, "driver"))
        asyncio.run(engine.drive(run_id, "driver"))
        self.assertEqual(db.get_run(run_id)["status"], "completed")
        self.assertEqual(len(set(calls)), 2)
        history = db.attempts(run_id)
        self.assertEqual([r["data"]["outcome"] for r in history], ["failure", "success"])
        self.assertEqual(history[0]["data"]["error"], "503 busy")

    def test_iterations_have_independent_attempt_counters(self):
        db, _, run_id, first_id = self.active()
        db.mutate_run(
            run_id,
            lambda r: r["nodes"]["work"].update(status="completed", outcome="success"),
            owner="driver",
            revision=0,
            attempt=("work", first_id),
        )
        db.mutate_run(
            run_id,
            lambda r: r["nodes"].update(
                work={
                    "node_id": "work",
                    "node_type": "agent_task",
                    "status": "pending",
                    "iteration": 1,
                }
            ),
            owner="driver",
            revision=0,
        )
        run = db.mutate_run(
            run_id,
            lambda r: r["nodes"]["work"].update(status="running"),
            owner="driver",
            revision=0,
        )
        self.assertEqual(run["nodes"]["work"]["iteration"], 1)
        self.assertEqual(run["nodes"]["work"]["attempt"], 1)
        self.assertNotEqual(run["nodes"]["work"]["attempt_id"], first_id)
        with self.assertRaises(ConflictError):
            db.mutate_run(
                run_id, lambda r: None, owner="driver", revision=0, attempt=("work", first_id)
            )
        self.assertEqual(
            [(a["iteration"], a["attempt"]) for a in db.attempts(run_id)], [(0, 1), (1, 1)]
        )

    def test_unstarted_cancellation_does_not_consume_an_attempt(self):
        db = Database(self.path)
        engine = Engine(db)
        run_id = engine.start(workflow())["run_id"]
        engine.cancel(run_id)
        self.assertEqual(db.attempts(run_id), [])
        engine.retry(run_id, workflow())
        self.assertTrue(db.claim(run_id, "driver"))
        run = db.mutate_run(
            run_id,
            lambda r: r["nodes"]["work"].update(status="running"),
            owner="driver",
            revision=2,
        )
        self.assertEqual(run["nodes"]["work"]["attempt"], 1)

    def test_retry_single_flight_conflict_rolls_back_revision_spec_and_history(self):
        db, engine, run_id, _ = self.active()
        engine.cancel(run_id)
        before = db.execution(run_id)
        history = db.attempts(run_id)
        engine.start(workflow())
        with self.assertRaises(ConflictError):
            engine.retry(run_id, workflow("must not replace the snapshot"))
        self.assertEqual(db.execution(run_id), before)
        self.assertEqual(db.attempts(run_id), history)

    def test_effect_intent_is_durable_and_key_payload_is_immutable(self):
        db, _, run_id, attempt_id = self.active()
        store = EffectStore(db)
        options = {"owner": "driver", "revision": 0, "node_id": "work", "attempt_id": attempt_id}
        first = store.prepare(run_id, "create", "kanban", {"title": "test"}, **options)
        second = EffectStore(Database(self.path)).prepare(
            run_id, "create", "kanban", {"title": "test"}, **options
        )
        self.assertEqual(first["effect_id"], second["effect_id"])
        self.assertEqual(second["status"], "prepared")
        with self.assertRaises(ConflictError):
            store.prepare(run_id, "create", "kanban", {"title": "different"}, **options)

    def test_crash_after_external_action_requires_explicit_reconciliation(self):
        db, _, run_id, attempt_id = self.active()
        db.release(run_id, "driver")
        code = """
import json,os,sys
from hermes_workflows.db import Database
from hermes_workflows.effects import EffectStore
db=Database(sys.argv[1]); run=sys.argv[2]; attempt=sys.argv[3]
assert db.claim(run,'crashed')
store=EffectStore(db)
effect=store.prepare(run,'create','kanban',{'title':'test'},owner='crashed',revision=0,node_id='work',attempt_id=attempt)
store.begin(effect['effect_id'],owner='crashed',revision=0)
store.attach_handle(effect['effect_id'],{'card':'card-1'},owner='crashed',revision=0)
os._exit(0)
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.path), run_id, attempt_id],
            env={**os.environ, "PYTHONPATH": str(PLUGIN)},
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Simulate expiry without making this crash test depend on wall-clock timing.
        with db.connect(write=True) as con:
            con.execute("UPDATE py_runs SET lease_until=0 WHERE run_id=?", (run_id,))
        self.assertTrue(db.claim(run_id, "replacement"))
        store = EffectStore(Database(self.path))
        unknown = store.recover(run_id, owner="replacement", revision=0)
        self.assertEqual(len(unknown), 1)
        effect = unknown[0]
        self.assertEqual(effect["handle"], {"card": "card-1"})
        with self.assertRaises(ConflictError):
            store.begin(effect["effect_id"], owner="replacement", revision=0)
        with self.assertRaises(ConflictError):
            store.complete(effect["effect_id"], {}, owner="crashed", revision=0)
        store.resolve(
            effect["effect_id"],
            {"found": "card-1"},
            completed=True,
            owner="replacement",
            revision=0,
        )
        self.assertEqual(store.get(effect["effect_id"])["status"], "completed")

    def test_effect_crash_before_ack_without_handle_is_still_unknown(self):
        db, _, run_id, _ = self.active()
        store = EffectStore(db)
        effect = store.prepare(
            run_id, "notice", "delivery", {"message": "test"}, owner="driver", revision=0
        )
        store.begin(effect["effect_id"], owner="driver", revision=0)
        db.release(run_id, "driver")
        self.assertTrue(db.claim(run_id, "new"))
        unknown = store.recover(run_id, owner="new", revision=0)
        self.assertEqual(unknown[0]["status"], "unknown")
        self.assertIsNone(unknown[0]["handle"])
        with self.assertRaises(ConflictError):
            store.begin(effect["effect_id"], owner="new", revision=0)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.home = contextvars.ContextVar("native-test-home")
        self.home.set(Path("original").resolve())
        self.original = self.home.get()
        self.loads = []
        self.modules = {
            "hermes_constants": SimpleNamespace(
                set_hermes_home_override=self.home.set,
                reset_hermes_home_override=self.home.reset,
                get_hermes_home=self.home.get,
            )
        }

    def loader(self, name):
        self.loads.append(name)
        if name not in self.modules:
            raise ModuleNotFoundError(name)
        return self.modules[name]

    def test_lazy_probe_reports_missing_apis_without_running_them(self):
        def load_config():
            raise AssertionError("probe must not load secrets/config")

        self.modules["hermes_cli.config"] = SimpleNamespace(load_config=load_config)
        host = NativeHost(Path("qa"), loader=self.loader)
        self.assertEqual(self.loads, [])
        report = host.probe()
        self.assertTrue(report["scope_available"])
        self.assertFalse(report["features"]["config"]["available"])
        self.assertEqual(report["features"]["config"]["missing"], ["save_config"])
        self.assertEqual(self.home.get(), self.original)

    def test_missing_host_fails_explicitly(self):
        self.modules.clear()
        host = NativeHost(Path("qa"), loader=self.loader)
        self.assertFalse(host.probe()["scope_available"])
        with self.assertRaises(NativeUnavailable):
            host.call("kanban", "create_task")

    def test_broken_profile_resolution_is_explicit_and_restores_scope(self):
        self.modules["hermes_constants"].get_hermes_home = lambda: None
        report = NativeHost(Path("qa"), loader=self.loader).probe()
        self.assertFalse(report["scope_available"])
        self.assertIn("could not resolve", report["scope_error"])
        self.assertEqual(self.home.get(), self.original)

    def test_profile_scope_restores_on_success_and_failure_without_env_changes(self):
        def load_config():
            return self.home.get()

        def save_config():
            raise ValueError("test failure")

        self.modules["hermes_cli.config"] = SimpleNamespace(
            load_config=load_config, save_config=save_config
        )
        env = dict(os.environ)
        host = NativeHost(Path("qa"), loader=self.loader)
        self.assertEqual(host.call("config", "load_config"), host.home)
        with self.assertRaises(ValueError):
            host.call("config", "save_config")
        self.assertEqual(self.home.get(), self.original)
        self.assertEqual(dict(os.environ), env)

    def test_async_profiles_do_not_leak_across_awaits(self):
        async def load_config():
            await asyncio.sleep(0)
            return self.home.get()

        self.modules["hermes_cli.config"] = SimpleNamespace(load_config=load_config)
        first = NativeHost(Path("qa-one"), loader=self.loader)
        second = NativeHost(Path("qa-two"), loader=self.loader)

        async def run():
            return await asyncio.gather(
                first.acall("config", "load_config"), second.acall("config", "load_config")
            )

        self.assertEqual(asyncio.run(run()), [first.home, second.home])
        self.assertEqual(self.home.get(), self.original)
        with self.assertRaisesRegex(TypeError, "acall"):
            first.call("config", "load_config")

    def test_probe_does_not_disclose_import_exception_text(self):
        def loader(name):
            if name == "hermes_constants":
                return self.modules[name]
            raise RuntimeError("DO_NOT_DISCLOSE_HOST_CONFIGURATION")

        report = NativeHost(Path("qa"), loader=loader).probe()
        self.assertNotIn("DO_NOT_DISCLOSE", json.dumps(report))
        self.assertFalse(any(item["available"] for item in report["features"].values()))
