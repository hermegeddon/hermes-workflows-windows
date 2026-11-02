from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.db import Database
from hermes_workflows.engine import Engine
from hermes_workflows.memory_adapter import HermesMemorySink
from hermes_workflows.workflow_memory import (
    Destination,
    MemoryOutbox,
    MemoryPolicy,
    MemoryUnavailable,
)


class Client:
    def __init__(self, home, plugin_id):
        self.home = Path(home)
        self.plugin_id = plugin_id
        self.destination = Destination(str(self.home), "hindsight", "a" * 64)
        self.calls = []
        self.receipt = {"status": "queued"}
        self.description = None

    def status(self):
        self.calls.append(("status", {}))
        return {"api_version": 1, "provider": self.destination.provider, "consented": True}

    def describe(self, context, *, timeout=5):
        self.calls.append(("describe", {"context": context, "timeout": timeout}))
        return (
            self.description
            if self.description is not None
            else {
                "status": "ready",
                "api_version": 1,
                "destination": asdict(self.destination),
                "capabilities": {"write": True, "lookup": False, "idempotency": False},
            }
        )

    def write(self, record, **kwargs):
        self.calls.append(("write", {"record": record, **kwargs}))
        return self.receipt

    def lookup(self, **kwargs):
        self.calls.append(("lookup", kwargs))
        return self.receipt


class MemoryAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory adapter żółć ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.client = Client(self.home, "hermes-workflows")
        self.context = {"session_id": "run", "cwd": "C:/example/project", "scheduled": False}

    def sink(self, *, hosted=False, client=None):
        selected = client or self.client
        if hosted:
            context = SimpleNamespace(
                memory_record_status=selected.status,
                describe_memory_writer=selected.describe,
                write_memory_record=selected.write,
                lookup_memory_record=selected.lookup,
            )
            return HermesMemorySink(
                self.home,
                self.context,
                context=context,
                loader=lambda name: self.fail("hosted bridge must use ctx"),
            )

        def loader(name):
            self.assertEqual(name, "hermes_cli.plugin_memory_records")

            def constructor(home, plugin_id):
                self.assertEqual(Path(home), self.home)
                self.assertEqual(plugin_id, "hermes-workflows")
                return selected

            return SimpleNamespace(MemoryRecordClient=constructor)

        return HermesMemorySink(self.home, self.context, loader=loader)

    def test_status_is_config_only_and_older_hosts_are_explicitly_unavailable(self):
        sink = self.sink()
        self.assertEqual(self.client.calls, [])
        status = sink.status()
        self.assertTrue(status["available"])
        self.assertEqual(status["provider_support"], "unprobed")
        self.assertEqual([name for name, _ in self.client.calls], ["status"])

        def old_host(name):
            raise ModuleNotFoundError(name)

        sink = HermesMemorySink(self.home, self.context, loader=old_host)
        self.assertEqual(sink.status()["reason"], "capability_unavailable")
        with self.assertRaises(MemoryUnavailable):
            asyncio.run(sink.describe())

    def test_in_process_and_hosted_calls_use_the_same_supported_contract(self):
        for hosted in (False, True):
            with self.subTest(hosted=hosted):
                self.client.calls.clear()
                sink = self.sink(hosted=hosted)
                destination = asyncio.run(sink.describe())
                self.assertEqual(destination, self.client.destination)
                self.assertEqual(
                    sink.capabilities, {"write": True, "lookup": False, "idempotency": False}
                )
                receipt = asyncio.run(
                    sink.write(
                        {"kind": "summary"},
                        effect_id="effect",
                        expected_destination=destination,
                        timeout=3,
                    )
                )
                self.assertEqual(receipt.status, "queued")
                call = self.client.calls[-1][1]
                self.assertEqual(call["context"], self.context)
                self.assertEqual(call["expected_destination"], asdict(destination))
                self.assertEqual(call["timeout"], 3)
                self.assertEqual(call["effect_id"], "effect")

    def test_provider_independence_has_no_vendor_branch_or_import(self):
        sink = self.sink()
        for provider in ("hindsight", "another_provider"):
            self.client.destination = replace(self.client.destination, provider=provider)
            self.assertEqual(asyncio.run(sink.describe()).provider, provider)
        self.assertEqual(len(self.client.calls), 2)

    def test_frozen_context_is_a_copy_and_wrong_home_is_refused(self):
        sink = self.sink()
        self.context["session_id"] = "changed"
        asyncio.run(sink.describe())
        self.assertEqual(self.client.calls[-1][1]["context"]["session_id"], "run")
        self.client.destination = replace(self.client.destination, home=str(self.home / "foreign"))
        with self.assertRaisesRegex(MemoryUnavailable, "wrong_profile"):
            asyncio.run(sink.describe())

    def test_unavailable_and_malformed_descriptions_do_not_write(self):
        sink = self.sink()
        for response in (
            {"status": "unavailable", "reason": "not_consented"},
            {"status": "unavailable", "reason": "capability_unavailable"},
            {"status": "unavailable", "reason": {"password": "must-not-leak"}},
            [],
            {"status": "ready", "api_version": 7},
        ):
            self.client.description = response
            with self.assertRaises(MemoryUnavailable) as caught:
                asyncio.run(sink.describe())
            self.assertNotIn("must-not-leak", str(caught.exception))
        self.assertFalse(any(name == "write" for name, _ in self.client.calls))

    def test_outbox_keeps_provider_status_and_reconciles_without_resending(self):
        db = Database(self.home / "workflows/runs.db")
        sink = self.sink()
        destination = asyncio.run(sink.describe())
        engine = Engine(
            db,
            memory_policy=MemoryPolicy(
                destination, enabled=True, write_node_events=True, writer_context=self.context
            ),
        )
        run = engine.start(
            {
                "id": "mem",
                "name": "Memory",
                "version": 1,
                "scope": {"type": "global"},
                "trigger": {"type": "manual"},
                "nodes": [{"id": "done", "type": "finish"}],
                "edges": [],
            },
            run_id="run",
        )
        outbox = MemoryOutbox(db)
        effect = outbox.entries(run["run_id"])[0]
        self.assertEqual(effect["payload"]["writer_context"], self.context)
        self.assertNotIn("cwd", effect["payload"]["record"])
        self.assertEqual(asyncio.run(outbox.deliver(effect["effect_id"], sink))["status"], "queued")
        self.client.receipt = {"status": "unknown", "reason": "lookup_unsupported"}
        self.assertEqual(
            asyncio.run(outbox.reconcile(effect["effect_id"], sink))["status"], "queued"
        )
        self.client.receipt = {"status": "unavailable", "reason": "writer_busy"}
        self.assertEqual(
            asyncio.run(outbox.reconcile(effect["effect_id"], sink))["status"], "queued"
        )
        self.assertEqual(sum(name == "write" for name, _ in self.client.calls), 1)

    def test_outbox_records_a_known_preacceptance_refusal(self):
        db = Database(self.home / "workflows/runs.db")
        sink = self.sink()
        destination = asyncio.run(sink.describe())
        engine = Engine(
            db, memory_policy=MemoryPolicy(destination, enabled=True, write_node_events=True)
        )
        engine.start(
            {
                "id": "mem",
                "name": "Memory",
                "version": 1,
                "scope": {"type": "global"},
                "trigger": {"type": "manual"},
                "nodes": [{"id": "done", "type": "finish"}],
                "edges": [],
            },
            run_id="run",
        )
        self.client.description = {"status": "unavailable", "reason": "not_consented"}
        outbox = MemoryOutbox(db)
        effect = outbox.entries("run")[0]
        row = asyncio.run(outbox.deliver(effect["effect_id"], sink))
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["result"], {"reason": "not_consented"})

    def test_malformed_acknowledgement_is_not_a_confirmed_write(self):
        sink = self.sink()
        destination = asyncio.run(sink.describe())
        self.client.receipt = {"status": "stored maybe", "password": "must-not-leak"}
        with self.assertRaises(ValueError):
            asyncio.run(
                sink.write({}, effect_id="effect", expected_destination=destination, timeout=2)
            )
        self.assertNotIn("must-not-leak", json.dumps(sink.status()))


if __name__ == "__main__":
    unittest.main()
