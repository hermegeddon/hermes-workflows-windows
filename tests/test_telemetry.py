from __future__ import annotations

import asyncio
import concurrent.futures
import copy
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
from hermes_workflows.api import API
from hermes_workflows.db import ConflictError, Database
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Result
from hermes_workflows.observer import associate, install, native_recorders
from hermes_workflows.redact import redact
from hermes_workflows.settings import save
from hermes_workflows.telemetry import NodeTelemetryRecorder, TelemetryStore
from hermes_workflows.trace import read_trace, transitions


def workflow():
    return dict(
        id="telemetry",
        name="Telemetry",
        version=1,
        scope={"type": "global"},
        trigger={"type": "manual"},
        nodes=[dict(id="work", type="agent_task", profile="default", prompt="capture")],
        edges=[],
    )


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow observations żółć ")
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "workflows/runs.db")
        self.engine = Engine(self.db)
        self.store = TelemetryStore(self.db.path)
        self.api = API(
            SimpleNamespace(db=self.db, specs=None, engine=self.engine, telemetry=self.store)
        )
        self.start()

    def start(self, trace=False):
        if trace:
            save(self.db, {"trace_enabled": True})
        state = self.engine.start(workflow())
        self.rid = state["run_id"]
        state = self.db.mutate_run(
            self.rid,
            lambda s: (s.update(status="running"), s["nodes"]["work"].update(status="running")),
        )
        self.att = state["nodes"]["work"]["attempt_id"]
        self.context = dict(
            db=str(self.db.path),
            run_id=self.rid,
            node_id="work",
            attempt_id=self.att,
            revision=state["execution_revision"],
            stream_key="direct",
        )

    def recorder(self, **changes):
        return NodeTelemetryRecorder(self.store, {**self.context, **changes})

    def pending(self, count=1):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.store.aggregate(self.att).get("approvals", [])
            pending = [a for a in result if a.get("can_respond")]
            if len(pending) == count:
                return pending
            time.sleep(0.01)
        self.fail("approval was not published")

    def decide(self, request, choice="once", **changes):
        body = dict(node_id="work", attempt_id=self.att, choice=choice)
        body.update(changes)
        return self.api.handle("POST", f"/runs/{self.rid}/approvals/{request['request_id']}", body)

    def test_pinned_reference_counters_errors_duration_and_approval(self):
        reference = json.loads(
            (PLUGIN.parent / "tests/fixtures/telemetry-reference.json").read_text()
        )
        clock = [0]
        recorder = NodeTelemetryRecorder(self.store, self.context, now=lambda: clock[0])
        for event, expected in zip(reference["events"], reference["snapshots"], strict=True):
            kind, clock[0], fields = event
            recorder.record(kind, **fields)
            actual = copy.deepcopy(self.store.aggregate(self.att))
            actual.pop("approvals", None)
            if "approval" in actual:
                actual["approval"].pop("can_respond", None)
            self.assertEqual(actual, expected)

    def test_live_projection_settlement_and_export_redact_before_storage(self):
        recorder = self.recorder()
        secret = "sk-" + "x" * 24
        recorder.record("api", usage={"input_tokens": 4, "output_tokens": 2}, api_request_id="a")
        recorder.record("api", usage={"input_tokens": 4}, api_request_id="a")
        recorder.record("tool", status="error", error_type="Test", error_message=f"token={secret}")
        self.assertNotIn("telemetry", self.db.get_run(self.rid)["nodes"]["work"])
        status, live = self.api.handle("GET", f"/runs/{self.rid}")
        self.assertEqual(status, 200)
        expected = live["nodes"]["work"]["telemetry"]
        self.assertEqual(expected["api_calls"], 1)
        self.assertEqual(expected["total_tokens"], 6)
        self.assertNotIn(secret, json.dumps(expected))
        state = self.db.mutate_run(
            self.rid,
            lambda s: (
                s.update(status="completed"),
                s["nodes"]["work"].update(
                    status="completed", outcome="success", output=f"password={secret}"
                ),
            ),
        )
        self.assertEqual(state["nodes"]["work"]["telemetry"], expected)
        self.assertEqual(self.db.attempts(self.rid)[0]["data"]["telemetry"], expected)
        with self.db.connect() as con:
            for table, column in (
                ("py_workers", "data"),
                ("py_runs", "state"),
                ("py_attempts", "data"),
            ):
                self.assertNotIn(secret, con.execute(f"SELECT {column} FROM {table}").fetchone()[0])
        exported = self.api.handle("GET", f"/runs/{self.rid}/export")[1]
        self.assertIn("attempts", exported["json"])
        self.assertNotIn(secret, json.dumps(exported))

    def test_stream_supersession_sequence_native_retry_and_card_aggregation(self):
        first = self.recorder(stream_key="card-1", native_run_id=1)
        first.record("api", usage={"input_tokens": 90})
        other = self.recorder(stream_key="card-2", native_run_id=1)
        other.record("api", usage={"input_tokens": 3})
        new = self.recorder(stream_key="card-1", native_run_id=2)
        new.record("api", usage={"input_tokens": 5})
        self.store.write(new.identity, {"input_tokens": 999}, 1)
        first.record("api", usage={"input_tokens": 1000})
        self.assertEqual(self.store.aggregate(self.att)["input_tokens"], 8)
        with self.assertRaises(ConflictError):
            self.recorder(stream_key="card-1", native_run_id=1)
        with self.assertRaises(ConflictError):
            self.store.write(first.identity, {}, 100)

    def test_retry_and_loop_histories_are_distinct_and_obsolete_writes_rejected(self):
        recorder = self.recorder()
        recorder.record("api", usage={"input_tokens": 6})
        updated = self.db.mutate_run(
            self.rid,
            lambda s: None,
            restart_attempts={"work": {"error": "retry", "telemetry": {"output_tokens": 1}}},
        )
        next_att = updated["nodes"]["work"]["attempt_id"]
        self.assertNotEqual(next_att, self.att)
        rows = self.db.attempts(self.rid)
        self.assertEqual(rows[0]["data"]["telemetry"]["input_tokens"], 6)
        self.assertNotIn("telemetry", rows[1]["data"])
        with self.assertRaises(ConflictError):
            self.store.write(recorder.identity, {}, 2)
        self.db.mutate_run(
            self.rid, lambda s: s["nodes"]["work"].update(status="pending", iteration=1)
        )
        updated = self.db.mutate_run(
            self.rid, lambda s: s["nodes"]["work"].update(status="running")
        )
        self.assertNotEqual(updated["nodes"]["work"]["attempt_id"], next_att)
        self.assertEqual(self.db.attempts(self.rid)[-1]["iteration"], 1)

    def test_native_approval_exact_binding_once_and_no_permanent_choice(self):
        recorder = self.recorder()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(recorder.approval, "fixture command", "confirm", timeout=3)
            request = self.pending()[0]
            self.assertEqual(self.decide(request, attempt_id="old")[0], 409)
            self.assertEqual(self.decide(request, "always")[0], 400)
            recorder.record("tool", status="success")  # Must not erase this prompt.
            self.assertTrue(self.pending()[0]["can_respond"])
            self.assertEqual(self.decide(request)[0], 200)
            self.assertEqual(result.result(3), "once")
            self.assertEqual(self.store.aggregate(self.att)["approval"]["choice"], "once")
            self.assertEqual(self.decide(request, "deny")[0], 409)
            recorder.record("approval_request", command="new native-only prompt", surface="cli")
            self.assertEqual(
                self.store.aggregate(self.att)["approval"]["command"], "new native-only prompt"
            )

    def test_concurrent_prompts_and_readonly_other_card_survive_projection(self):
        recorder = self.recorder()
        other = self.recorder(stream_key="card-2")
        other.record("approval_request", command="native only", surface="gateway")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            one = pool.submit(recorder.approval, "first", "confirm", timeout=3)
            two = pool.submit(recorder.approval, "second", "confirm", timeout=3)
            requests = self.pending(2)
            self.assertEqual(len(self.store.aggregate(self.att)["approvals"]), 3)
            for request in requests:
                self.assertEqual(self.decide(request, "deny")[0], 200)
            self.assertEqual([one.result(3), two.result(3)], ["deny", "deny"])

    def test_timeout_cancel_and_missing_store_fail_closed(self):
        recorder = self.recorder()
        self.assertEqual(recorder.approval("fixture", "confirm", timeout=0.01), "deny")
        self.assertEqual(self.store.aggregate(self.att)["approval"]["choice"], "timeout")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(recorder.approval, "fixture", "confirm", timeout=3)
            request = self.pending()[0]
            self.engine.cancel(self.rid)
            self.assertEqual(self.decide(request)[0], 409)
            self.assertEqual(result.result(3), "deny")
            saved = self.db.get_run(self.rid)["nodes"]["work"]["telemetry"]
            projected = self.api.handle("GET", f"/runs/{self.rid}")[1]["nodes"]["work"]["telemetry"]
            self.assertEqual(projected, saved)
            self.assertEqual(self.db.attempts(self.rid)[0]["data"]["telemetry"], saved)
            self.assertFalse(
                self.api.handle("GET", f"/runs/{self.rid}")[1]["nodes"]["work"]["telemetry"][
                    "approval"
                ]["can_respond"]
            )
        with patch.object(self.store, "connect", side_effect=OSError("unavailable")):
            recorder.record("api", usage={"input_tokens": 100})
            self.assertEqual(recorder.approval("fixture", "confirm"), "deny")

    def test_answer_not_consumed_before_deadline_cannot_authorize(self):
        recorder = self.recorder()
        sleeping, resume = threading.Event(), threading.Event()
        clock = [0]

        def pause(_seconds):
            sleeping.set()
            resume.wait(3)

        with (
            patch("hermes_workflows.telemetry.time.monotonic", lambda: clock[0]),
            patch("hermes_workflows.telemetry.time.sleep", pause),
        ):
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(recorder.approval, "fixture", "confirm", timeout=2)
                try:
                    self.assertTrue(sleeping.wait(3))
                    request = self.store.aggregate(self.att)["approval"]
                    self.assertEqual(self.decide(request)[0], 200)
                    clock[0] = 3
                finally:
                    resume.set()
                self.assertEqual(future.result(3), "deny")

    def test_trace_optin_pinned_fields_and_optional_failure(self):
        self.assertIsNone(read_trace(self.db, self.rid))
        self.engine.cancel(self.rid)
        self.start(trace=True)
        recorder = self.recorder()
        recorder.record("api", usage={"input_tokens": 3})
        before = dict(
            run_id=self.rid, status="running", nodes={"work": dict(status="scheduled")}, notified=[]
        )
        after = dict(
            run_id=self.rid,
            status="completed",
            observability={"trace_enabled": True},
            nodes={"work": dict(status="completed", outcome="success", seq=1)},
            notified=["completed"],
        )
        with self.db.connect(write=True) as con:
            transitions(con, before, after)
        reference = json.loads(
            (PLUGIN.parent / "tests/fixtures/telemetry-reference.json").read_text()
        )
        lines = [json.loads(line) for line in read_trace(self.db, self.rid).splitlines()]
        projected = [
            {
                k: v
                for k, v in e.items()
                if k not in ("ts", "run_id", "revision", "iteration", "attempt")
            }
            for e in lines[-3:]
        ]
        self.assertEqual(projected, reference["trace"])
        self.assertIn("attempt_id", next(e for e in lines if e["kind"] == "worker_telemetry"))
        with patch("hermes_workflows.trace.emit", side_effect=OSError("disk unavailable")):
            self.engine.cancel(self.rid)
        self.assertEqual(self.db.get_run(self.rid)["status"], "cancelled")

    def test_legacy_exports_keep_redacted_events_and_trace_failure_is_optional(self):
        self.db.mutate_run(self.rid, lambda state: state.pop("observability"))
        save(self.db, {"trace_enabled": True})
        legacy = read_trace(self.db, self.rid)
        self.assertEqual(json.loads(legacy.splitlines()[0])["kind"], "legacy_event")
        with self.db.connect(write=True) as con:
            con.execute("DROP TABLE py_traces")
        status, exported = self.api.handle("GET", f"/runs/{self.rid}/export")
        self.assertEqual(status, 200)
        self.assertNotIn("trace", exported)

    def test_cross_process_writer_and_wrong_profile_are_fenced(self):
        code = "import json,sys; from hermes_workflows.telemetry import *; c=json.loads(sys.argv[1]); r=NodeTelemetryRecorder(TelemetryStore(c['db']),c); r.record('api',usage={'input_tokens':11})"
        process = subprocess.run(
            [sys.executable, "-c", code, json.dumps(self.context)],
            env={**os.environ, "PYTHONPATH": str(PLUGIN)},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(self.store.aggregate(self.att)["input_tokens"], 11)
        self.db.mutate_run(
            self.rid,
            lambda s: s["notification_context"].update(
                home=str(Path(self.temp.name) / "another-profile")
            ),
        )
        with self.assertRaises(ConflictError):
            self.recorder()

    def test_native_association_is_transactional_and_optional_hooks_rollback(self):
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as native:
            native.execute("BEGIN")
            associate(native, self.db.path, self.rid, "work", self.att, 0, "card")
            self.assertEqual(
                native.execute("SELECT task_id FROM hermes_workflows_observers").fetchone()[0],
                "card",
            )
            native.rollback()
            self.assertIsNone(
                native.execute(
                    "SELECT name FROM sqlite_master WHERE name='hermes_workflows_observers'"
                ).fetchone()
            )
        handles = []

        def hook(name, callback):
            handle = SimpleNamespace(dispose=lambda: handles.remove(handle))
            handles.append(handle)
            return handle

        ctx = SimpleNamespace(
            register_hook=hook,
            register_approval_transport=lambda *a: (_ for _ in ()).throw(
                RuntimeError("registration failed")
            ),
        )
        with self.assertRaises(RuntimeError):
            install(ctx, [self.recorder()])
        self.assertEqual(handles, [])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(native_recorders(), [])

    def test_engine_settlement_uses_live_observer_metrics(self):
        self.engine.cancel(self.rid)

        class Executor:
            home = None

            async def execute(inner, node, run, config):
                recorder = NodeTelemetryRecorder(self.store, node["_telemetry_context"])
                recorder.record("api", usage={"input_tokens": 9, "output_tokens": 1})
                recorder.record("tool", status="success")
                return Result("success", output="done", telemetry={"duration_ms": 99})

        self.engine = Engine(self.db, Executor())
        self.rid = self.engine.start(workflow())["run_id"]
        self.assertTrue(self.db.claim(self.rid, "test-owner"))
        asyncio.run(self.engine.drive(self.rid, "test-owner"))
        state = self.db.get_run(self.rid)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["nodes"]["work"]["telemetry"]["total_tokens"], 10)
        self.assertEqual(self.db.attempts(self.rid)[0]["data"]["telemetry"]["tool_calls"], 1)

    def test_redaction_preserves_counters_and_scrubs_nested_captures(self):
        data = redact(
            {
                "input_tokens": 4,
                "api_key": "hidden",
                "nested": ["Bearer abc.def", "password='with spaces'", "ghp_" + "a" * 25],
            }
        )
        self.assertEqual(data["input_tokens"], 4)
        self.assertEqual(data["api_key"], "[REDACTED]")
        self.assertNotIn("with spaces", json.dumps(data))
        self.assertEqual(
            redact({"access_token": "hidden", "output_tokens": 9}),
            {"access_token": "[REDACTED]", "output_tokens": 9},
        )
        self.assertNotIn("hidden", redact("AWS_SECRET_ACCESS_KEY=hidden"))
        recorder = self.recorder()
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n" + "PRIVATE" * 1000 + "\n-----END PRIVATE KEY-----"
        )
        recorder.record("tool", status="error", error_message=private_key)
        self.assertEqual(self.store.aggregate(self.att)["error_message"], "[REDACTED]")

    def test_schema3_upgrade_is_additive_and_failed_ddl_rolls_back(self):
        before = self.db.get_run(self.rid)
        with self.db.connect(write=True) as con:
            con.execute("DROP TRIGGER py_schedule_authority_guard")
            for table in ("py_worker_approvals", "py_workers", "py_traces"):
                con.execute(f"DROP TABLE {table}")
            con.execute("UPDATE py_metadata SET version=3")

        def interrupted(con, sql):
            con.execute("CREATE TABLE py_workers(incomplete)")
            raise OSError("simulated interrupted upgrade")

        with patch("hermes_workflows.migrations.statements", interrupted):
            with self.assertRaises(OSError):
                Database(self.db.path)
        with self.db.connect() as con:
            self.assertEqual(con.execute("SELECT version FROM py_metadata").fetchone()[0], 3)
            self.assertIsNone(
                con.execute("SELECT name FROM sqlite_master WHERE name='py_workers'").fetchone()
            )
        self.assertEqual(Database(self.db.path).get_run(self.rid), before)

    def test_locked_worker_store_is_bounded_and_recovers_without_breaking_caller(self):
        recorder = self.recorder()
        with self.db.connect(write=True):
            start = time.monotonic()
            recorder.record("api", usage={"input_tokens": 2})
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(recorder.approval("fixture", "confirm"), "deny")
        recorder.record("api", usage={"input_tokens": 3})
        self.assertEqual(self.store.aggregate(self.att)["input_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
