from __future__ import annotations

import asyncio
import copy
import itertools
import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.db import ConflictError, Database
from hermes_workflows.effects import EffectStore
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Executor, Result
from hermes_workflows.workflow_memory import Destination, MemoryOutbox, MemoryPolicy, Receipt


def workflow():
    return {
        "id": "memory-test",
        "name": "Memory test",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "worker-profile"},
        "nodes": [{"id": "work", "type": "agent_task", "prompt": "private prompt"}],
        "edges": [],
    }


class FakeSink:
    def __init__(self, destination, receipt=None):
        self.destination = destination
        self.receipt = receipt or Receipt("queued", "receipt-1")
        self.writes, self.lookups = [], []
        self.before_return = None

    async def describe(self):
        await asyncio.sleep(0)
        return self.destination

    async def write(self, record, **kwargs):
        self.assert_destination(kwargs["expected_destination"])
        self.writes.append((record, kwargs))
        if self.before_return:
            await self.before_return()
        return self.receipt

    async def lookup(self, **kwargs):
        self.assert_destination(kwargs["expected_destination"])
        self.lookups.append(kwargs)
        return self.receipt

    def assert_destination(self, expected):
        if expected != self.destination:
            raise RuntimeError("binding changed before acceptance")


class WorkflowMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow memory żółć ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.db = Database(self.home / "workflows/runs.db")
        self.destination = Destination(str(self.home), "fake", "a" * 64)
        self.policy = MemoryPolicy(self.destination, enabled=True, write_node_events=True)
        self.outbox = MemoryOutbox(self.db)

    def engine(self, policy=None, runner=None):
        async def success(*_):
            return Result("success", "private output")

        return Engine(
            self.db, Executor(agent_runner=runner or success), memory_policy=policy or self.policy
        )

    def start(self, *, policy=None, spec=None):
        engine = self.engine(policy)
        run = engine.start(spec or workflow())
        return engine, run["run_id"]

    def drive(self, engine, run_id):
        self.assertTrue(self.db.claim(run_id, "driver"))
        asyncio.run(engine.drive(run_id, "driver"))
        return self.db.get_run(run_id)

    def kinds(self, run_id):
        return [row["payload"]["record"]["kind"] for row in self.outbox.entries(run_id)]

    def test_dormant_default_and_disabled_policy_do_not_capture_or_import_a_provider(self):
        for engine in (Engine(self.db), self.engine(replace(self.policy, enabled=False))):
            run_id = engine.start(workflow())["run_id"]
            engine.cancel(run_id)
            self.assertNotIn("memory_export", self.db.get_run(run_id))
            self.assertEqual(self.outbox.entries(run_id), [])

    def test_all_policy_flag_combinations(self):
        for summaries, failures, events in itertools.product((False, True), repeat=3):
            with self.subTest(summaries=summaries, failures=failures, events=events):
                policy = replace(
                    self.policy,
                    write_run_summaries=summaries,
                    write_node_failures=failures,
                    write_node_events=events,
                )
                engine, run_id = self.start(policy=policy)
                self.db.mutate_run(
                    run_id, lambda run: run["nodes"]["work"].update(status="running")
                )

                def fail(run):
                    run["nodes"]["work"].update(status="completed", outcome="failure")
                    run.update(status="failed", finished_at=time.time())

                self.db.mutate_run(run_id, fail)
                kinds = self.kinds(run_id)
                self.assertEqual(kinds.count("run_started"), int(events))
                self.assertEqual(kinds.count("node_started"), int(events))
                self.assertEqual(kinds.count("node_completed"), int(events))
                self.assertEqual(kinds.count("node_failed"), int(failures))
                self.assertEqual(kinds.count("run_completed"), int(summaries))
                self.assertEqual(kinds.count("retrospective"), int(summaries))

    def test_success_and_cancellation_summaries_are_distinct_from_durable_receipts(self):
        for status in ("completed", "cancelled"):
            engine, run_id = self.start()
            run = self.drive(engine, run_id) if status == "completed" else engine.cancel(run_id)
            self.assertEqual(run["status"], status)
            entries = self.outbox.entries(run_id)
            terminal = [
                r["payload"]["record"]
                for r in entries
                if r["payload"]["record"]["kind"] == "run_completed"
            ]
            self.assertEqual([r["status"] for r in terminal], [status])
            self.assertTrue(all(row["status"] == "prepared" for row in entries))
            self.assertTrue(all("node_counts" in r for r in terminal))

    def test_default_payload_minimizes_and_redacts_before_persistence_and_delivery(self):
        secret = "sk-" + "X" * 24
        spec = workflow()
        spec["id"] = secret
        spec["nodes"][0]["prompt"] = "password=private-prompt"
        engine, run_id = self.start(spec=spec)
        self.drive(engine, run_id)
        entries = self.outbox.entries(run_id)
        records = [row["payload"]["record"] for row in entries]
        encoded = json.dumps(records)
        for private in (secret, "private-prompt", "private output", str(self.home)):
            self.assertNotIn(private, encoded)
        self.assertIn("[REDACTED]", encoded)
        sink = FakeSink(self.destination)
        for row in entries:
            asyncio.run(self.outbox.deliver(row["effect_id"], sink))
        self.assertNotIn(secret, json.dumps([record for record, _ in sink.writes]))

    def test_run_owner_binding_is_frozen_and_cannot_be_rebound_by_mutation(self):
        other = replace(self.destination, home=str(self.home / "other"))
        with self.assertRaisesRegex(ValueError, "run-owner"):
            self.start(policy=replace(self.policy, destination=other))
        engine, run_id = self.start()
        with self.assertRaisesRegex(ConflictError, "cannot be rebound"):
            self.db.mutate_run(
                run_id, lambda run: run["memory_export"]["destination"].update(binding="b" * 64)
            )
        self.assertEqual(
            self.db.get_run(run_id)["memory_export"]["destination"]["binding"], "a" * 64
        )
        engine.cancel(run_id)

    def test_intent_transaction_rolls_back_with_run_and_capture_errors_are_fail_open(self):
        engine, run_id = self.start()
        before = self.outbox.entries(run_id)
        with self.assertRaises(RuntimeError), self.db.connect(write=True) as con:
            from hermes_workflows.workflow_memory import capture

            run = self.db.get_run(run_id)
            run["status"] = "failed"
            capture(con, self.db.get_run(run_id), run)
            raise RuntimeError("abort transaction")
        self.assertEqual(self.outbox.entries(run_id), before)
        with patch(
            "hermes_workflows.workflow_memory.capture", side_effect=RuntimeError("secret details")
        ):
            with self.assertLogs("hermes_workflows.db", level="WARNING") as logs:
                self.assertEqual(self.drive(engine, run_id)["status"], "completed")
        self.assertNotIn("secret details", " ".join(logs.output))

    def test_loop_attempts_have_distinct_ids_and_repeated_transitions_do_not_duplicate(self):
        spec = workflow()
        spec["nodes"] += [
            {"id": "start", "type": "prompt", "prompt": "Begin"},
            {"id": "review", "type": "human_review"},
            {"id": "done", "type": "finish"},
        ]
        spec["edges"] = [
            {"from": "start", "to": "work"},
            {"from": "work", "to": "review"},
            {
                "from": "review",
                "to": "work",
                "condition": {"type": "review_status", "equals": "needs_changes"},
            },
            {
                "from": "review",
                "to": "done",
                "condition": {"type": "review_status", "equals": "approved"},
            },
        ]
        engine, run_id = self.start(spec=spec)
        self.assertEqual(self.drive(engine, run_id)["status"], "waiting")
        engine.review(run_id, "review", "needs_changes")
        self.assertEqual(self.drive(engine, run_id)["status"], "waiting")
        engine.review(run_id, "review", "approved")
        self.assertEqual(self.drive(engine, run_id)["status"], "completed")
        events = [
            row
            for row in self.outbox.entries(run_id)
            if row["payload"]["record"].get("node_id") == "work"
            and row["payload"]["record"]["kind"] == "node_completed"
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual({r["payload"]["record"]["iteration"] for r in events}, {0, 1})
        snapshot = self.outbox.entries(run_id)
        self.db.mutate_run(run_id, lambda run: run.update(unrelated_marker=True))
        self.assertEqual(self.outbox.entries(run_id), snapshot)

    def test_archived_automatic_retry_failure_is_not_lost(self):
        _, run_id = self.start()
        self.db.mutate_run(run_id, lambda run: run["nodes"]["work"].update(status="running"))
        self.db.mutate_run(
            run_id,
            lambda run: run["nodes"]["work"].update(status="running", started_at=time.time()),
            restart_attempts={"work": {"outcome": "failure", "error": "private failure"}},
        )
        failed = [
            row["payload"]["record"]
            for row in self.outbox.entries(run_id)
            if row["payload"]["record"]["kind"] == "node_failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertNotIn("error", failed[0])

    def test_retry_and_resume_fence_old_intents_without_rebinding_destination(self):
        for operation in ("retry", "resume"):
            engine, run_id = self.start()
            self.db.mutate_run(run_id, lambda run: run.update(status="failed"))
            old = self.outbox.entries(run_id)[0]
            if operation == "resume":
                engine.resume(run_id, workflow(), reset_all=True)
            else:
                engine.retry(run_id, workflow())
            sink = FakeSink(self.destination)
            self.assertEqual(
                asyncio.run(self.outbox.deliver(old["effect_id"], sink))["status"], "stale"
            )
            self.assertEqual(sink.writes, [])
            self.assertEqual(self.drive(engine, run_id)["status"], "completed")
            self.assertEqual({r["revision"] for r in self.outbox.entries(run_id)}, {0, 1})

    def test_two_drivers_send_a_prepared_intent_only_once(self):
        _, run_id = self.start()
        effect = self.outbox.entries(run_id)[0]
        sink = FakeSink(self.destination)
        other = MemoryOutbox(Database(self.db.path))

        async def concurrent():
            await asyncio.gather(
                self.outbox.deliver(effect["effect_id"], sink),
                other.deliver(effect["effect_id"], sink),
            )

        asyncio.run(concurrent())
        self.assertEqual(len(sink.writes), 1)
        self.assertEqual(self.outbox.effects.get(effect["effect_id"])["status"], "queued")

    def test_queued_is_not_durable_and_lookup_never_writes(self):
        _, run_id = self.start()
        effect = self.outbox.entries(run_id)[0]
        sink = FakeSink(self.destination)
        result = asyncio.run(self.outbox.deliver(effect["effect_id"], sink))
        self.assertEqual(result["status"], "queued")
        sink.receipt = Receipt("unknown")
        self.assertEqual(
            asyncio.run(self.outbox.reconcile(effect["effect_id"], sink))["status"], "queued"
        )
        sink.receipt = Receipt("acknowledged", "durable-1")
        self.assertEqual(
            asyncio.run(self.outbox.reconcile(effect["effect_id"], sink))["status"], "acknowledged"
        )
        self.assertEqual(len(sink.writes), 1)
        self.assertNotIn("search_visible", self.outbox.effects.get(effect["effect_id"])["result"])

    def test_timeout_after_external_acceptance_is_unknown_and_never_resent(self):
        engine, run_id = self.start()
        self.drive(engine, run_id)
        snapshot = self.db.get_run(run_id)
        effect = self.outbox.entries(run_id)[0]
        sink = FakeSink(self.destination)
        sink.before_return = lambda: asyncio.sleep(1)
        result = asyncio.run(self.outbox.deliver(effect["effect_id"], sink, timeout=0.01))
        self.assertEqual(result["status"], "unknown")
        restarted = MemoryOutbox(Database(self.db.path))
        asyncio.run(restarted.deliver(effect["effect_id"], sink))
        self.assertEqual(len(sink.writes), 1)
        self.assertEqual(self.db.get_run(run_id), snapshot)
        sink.receipt = Receipt("acknowledged")
        self.assertEqual(
            asyncio.run(restarted.reconcile(effect["effect_id"], sink))["status"], "acknowledged"
        )

    def test_interrupted_delivery_recovers_unknown_and_late_ack_cannot_overwrite_it(self):
        _, run_id = self.start()
        effect = self.outbox.entries(run_id)[0]
        sink = FakeSink(self.destination, Receipt("acknowledged"))

        async def expire():
            with self.db.connect(write=True) as con:
                con.execute(
                    "UPDATE py_effects SET handle=? WHERE effect_id=?",
                    (json.dumps({"deadline": 0}), effect["effect_id"]),
                )
            self.assertEqual(MemoryOutbox(Database(self.db.path)).recover_expired(), 1)

        sink.before_return = expire
        self.assertEqual(
            asyncio.run(self.outbox.deliver(effect["effect_id"], sink))["status"], "unknown"
        )
        self.assertEqual(len(sink.writes), 1)

    def test_run_lease_recovery_does_not_recover_a_live_memory_delivery(self):
        _, run_id = self.start()
        effect = self.outbox.entries(run_id)[0]
        sink = FakeSink(self.destination)

        async def recover_run():
            self.assertTrue(self.db.claim(run_id, "other-driver"))
            EffectStore(self.db).recover(run_id, owner="other-driver", revision=0)
            self.assertEqual(self.outbox.effects.get(effect["effect_id"])["status"], "in_flight")

        sink.before_return = recover_run
        self.assertEqual(
            asyncio.run(self.outbox.deliver(effect["effect_id"], sink))["status"], "queued"
        )

    def test_changed_or_unavailable_destination_never_writes(self):
        for destination, expected in (
            (None, "unavailable"),
            (replace(self.destination, binding="b" * 64), "stale"),
        ):
            engine, run_id = self.start()
            effect = self.outbox.entries(run_id)[0]
            sink = FakeSink(destination)
            self.assertEqual(
                asyncio.run(self.outbox.deliver(effect["effect_id"], sink))["status"], expected
            )
            self.assertEqual(sink.writes, [])
            engine.cancel(run_id)

    def test_a_second_provider_uses_the_identical_record_and_sink_contract(self):
        records = []
        for provider in ("fake", "another_provider"):
            destination = replace(self.destination, provider=provider)
            engine, run_id = self.start(policy=replace(self.policy, destination=destination))
            effect = self.outbox.entries(run_id)[0]
            sink = FakeSink(destination, Receipt("acknowledged"))
            self.assertEqual(
                asyncio.run(self.outbox.deliver(effect["effect_id"], sink))["status"],
                "acknowledged",
            )
            record = copy.deepcopy(sink.writes[0][0])
            record.pop("run_id")
            records.append(record)
            engine.cancel(run_id)
        self.assertEqual(records[0], records[1])

    def test_malformed_and_exceptional_receipts_are_unknown_without_secret_error_text(self):
        for mode in ("malformed", "exception", "failed"):
            engine, run_id = self.start()
            effect = self.outbox.entries(run_id)[0]
            sink = FakeSink(self.destination)
            sink.receipt = None if mode == "malformed" else Receipt("failed")

            async def fail():
                raise RuntimeError("password=must-not-leak")

            if mode == "exception":
                sink.before_return = fail
            result = asyncio.run(self.outbox.deliver(effect["effect_id"], sink))
            self.assertEqual(result["status"], "failed" if mode == "failed" else "unknown")
            self.assertNotIn("must-not-leak", json.dumps(result))
            engine.cancel(run_id)

    def test_cancellation_marks_unknown_before_propagating(self):
        _, run_id = self.start()
        effect = self.outbox.entries(run_id)[0]
        sink = FakeSink(self.destination)

        async def cancel():
            started = asyncio.Event()

            async def hang():
                started.set()
                await asyncio.sleep(10)

            sink.before_return = hang
            task = asyncio.create_task(self.outbox.deliver(effect["effect_id"], sink))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancel())
        self.assertEqual(self.outbox.effects.get(effect["effect_id"])["status"], "unknown")
        self.assertEqual(len(sink.writes), 1)

    def test_native_worker_completion_records_keep_the_orchestrator_owner_and_exclude_output(self):
        from hermes_workflows.kanban import KanbanExecutor
        from test_kanban import BoardDouble
        from test_kanban import workflow as board_workflow

        boards = BoardDouble(self.home / "foreign-native-owner")
        engine = self.engine()
        engine.kanban = KanbanExecutor(self.db, boards.host(), poll_seconds=0.01)
        run = engine.start(board_workflow())
        run_id = run["run_id"]
        self.assertTrue(self.db.claim(run_id, "native-memory-qa"))

        async def complete():
            async with asyncio.timeout(20):
                task = asyncio.create_task(engine.drive(run_id, "native-memory-qa"))
                try:
                    while not boards.created:
                        await asyncio.sleep(0.01)
                    boards.finish(boards.cards()[0]["id"], output="private native worker output")
                    await task
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

        asyncio.run(complete())
        self.assertEqual(self.db.get_run(run_id)["status"], "completed")
        self.assertEqual(len(boards.created), 1)
        entries = self.outbox.entries(run_id)
        completed = [
            row
            for row in entries
            if row["payload"]["record"]["kind"] == "node_completed"
            and row["payload"]["record"].get("node_id") == "work"
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["payload"]["destination"]["home"], str(self.home))
        self.assertNotIn(
            "private native worker output",
            json.dumps([row["payload"]["record"] for row in entries]),
        )
        sink = FakeSink(self.destination)
        for row in entries:
            asyncio.run(self.outbox.deliver(row["effect_id"], sink))
        self.assertEqual(len(sink.writes), len(entries))


if __name__ == "__main__":
    unittest.main()
