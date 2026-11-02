from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.api import API
from hermes_workflows.db import Database
from hermes_workflows.engine import Engine
from hermes_workflows.memory_service import MemoryService, policy
from hermes_workflows.runtime import Runtime
from hermes_workflows.settings import validate, values
from hermes_workflows.specs import SpecStore
from hermes_workflows.workflow_memory import Destination, MemoryUnavailable, Receipt


def workflow():
    return {
        "id": "memory-policy",
        "name": "Memory policy",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "foreign-worker"},
        "nodes": [{"id": "done", "type": "finish"}],
        "edges": [],
    }


class Sink:
    def __init__(self, owner, writer_context, *, context, timeout):
        self.owner = owner
        self.writer_context = copy.deepcopy(writer_context)
        self.context, self.timeout = context, timeout
        self.capabilities = {"write": True, "lookup": False, "idempotency": False}

    def status(self):
        return {
            "available": True,
            "provider": "hindsight",
            "consented": True,
            "provider_support": "unprobed",
        }

    def bind(self):
        self.owner.binds.append(self.writer_context)
        if self.owner.unavailable:
            raise MemoryUnavailable(self.owner.unavailable)
        self.writer_context.setdefault("cwd", "C:/resolved/profile-project")
        return Destination(str(self.owner.home), "hindsight", "b" * 64)

    async def describe(self):
        return self.bind()

    async def write(self, record, **kwargs):
        self.owner.writes.append((record, kwargs))
        if len(self.owner.writes) >= 4:
            self.owner.delivered.set()
        return Receipt("queued")

    async def lookup(self, **kwargs):
        self.owner.lookups.append(kwargs)
        return Receipt("unknown", reason="lookup_unsupported")


class MemorySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory settings żółć ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = Database(self.home / "workflows/runs.db")
        self.binds, self.writes, self.lookups = [], [], []
        self.unavailable = None
        self.delivered = threading.Event()
        self.service = MemoryService(self.db, sink_factory=self.factory)
        self.engine = Engine(self.db, memory_policy=self.service)

    def factory(self, home, writer_context, *, context, timeout):
        self.assertEqual(home, self.home)
        return Sink(self, writer_context, context=context, timeout=timeout)

    def drive(self, run_id):
        self.assertTrue(self.db.claim(run_id, "test"))
        asyncio.run(self.engine.drive(run_id, "test"))

    def test_fresh_and_legacy_auto_settings_remain_dormant(self):
        self.db.save_settings({"mode": "auto", "fail_open": False, "write_node_events": True})
        with patch.dict("os.environ", {"HERMES_WORKFLOWS_MEMORY_PROVIDER": "auto"}):
            config = values(self.db)
            self.assertFalse(config["workflow_memory_enabled"])
            self.assertEqual(config["mode"], "auto")
            run = self.engine.start(workflow())
        self.assertNotIn("memory_export", run)
        self.assertEqual(self.binds, [])
        self.assertEqual(self.service.outbox.entries(run["run_id"]), [])

    def test_explicit_enablement_freezes_owner_origin_and_context_without_changing_history(self):
        old = self.engine.start(workflow())
        self.drive(old["run_id"])
        with self.db.connect() as con:
            before = dict(
                con.execute(
                    "SELECT state,spec FROM py_runs WHERE run_id=?", (old["run_id"],)
                ).fetchone()
            )
        self.db.save_settings(
            {"workflow_memory_enabled": True, "workflow_memory_write_node_events": True}
        )
        spec = workflow()
        spec["defaults"]["workdir"] = "C:/run-owner/project"
        run = self.engine.start(
            spec,
            origin="discord:test-channel",
            origin_metadata={"user_id": "user-1", "session_key": "parent-session"},
        )
        frozen = run["memory_export"]
        self.assertEqual(frozen["destination"]["home"], str(self.home))
        self.assertEqual(frozen["writer_context"]["platform"], "discord")
        self.assertEqual(frozen["writer_context"]["user_id"], "user-1")
        self.assertEqual(frozen["writer_context"]["parent_session_id"], "parent-session")
        self.assertEqual(frozen["writer_context"]["cwd"], "C:/run-owner/project")
        with self.db.connect() as con:
            self.assertEqual(
                before,
                dict(
                    con.execute(
                        "SELECT state,spec FROM py_runs WHERE run_id=?", (old["run_id"],)
                    ).fetchone()
                ),
            )

    def test_per_workflow_overrides_do_not_reinterpret_legacy_provider_names(self):
        self.db.save_settings({"workflow_memory_enabled": True})
        for provider in ("auto", "open_second_brain"):
            spec = workflow()
            spec["defaults"]["memory"] = {"provider": provider}
            run = self.engine.start(spec)
            self.assertEqual(run["memory_export"]["reason"], "unsupported_provider")
            self.engine.cancel(run["run_id"])
        self.assertEqual(self.binds, [])
        spec = workflow()
        spec["defaults"]["workflow_memory"] = {"enabled": False, "provider": "hermes"}
        run = self.engine.start(spec)
        self.assertNotIn("memory_export", run)

    def test_workflow_can_opt_in_without_enabling_the_global_default(self):
        spec = workflow()
        spec["defaults"]["workflow_memory"] = {
            "enabled": True,
            "provider": "hermes",
            "write_node_events": True,
        }
        run = self.engine.start(spec)
        self.assertTrue(run["memory_export"]["enabled"])
        self.assertEqual(len(self.service.outbox.entries(run["run_id"])), 1)

    def test_native_policy_is_independent_of_legacy_policy_and_preview_preserves_route(self):
        spec = workflow()
        spec["defaults"]["memory"] = {"provider": "none"}
        spec["defaults"]["workflow_memory"] = {"provider": "hermes", "enabled": True}
        run = self.engine.start(spec)
        self.assertTrue(run["memory_export"]["enabled"])
        self.assertEqual(self.db.get_spec(run["run_id"])["defaults"], spec["defaults"])
        self.assertEqual(self.service.preview(spec)["route"], "hermes")
        spec["defaults"]["workflow_memory"] = {"provider": "none"}
        self.assertEqual(self.service.preview(spec)["route"], "none")
        self.assertFalse(self.service.preview(spec)["enabled"])

    def test_unsupported_strict_policy_and_invalid_values_never_bind(self):
        self.db.save_settings({"workflow_memory_enabled": True})
        for local in ({"fail_open": False}, {"enabled": "true"}, {"write_node_events": "yes"}):
            spec = workflow()
            spec["defaults"]["workflow_memory"] = local
            run = self.engine.start(spec)
            self.assertFalse(run["memory_export"]["enabled"])
            self.assertIn(
                run["memory_export"]["reason"], ("strict_mode_unsupported", "invalid_policy")
            )
            self.engine.cancel(run["run_id"])
        self.assertEqual(self.binds, [])
        for value in (0, 31, -1):
            with self.assertRaises(ValueError):
                validate({"workflow_memory_timeout_seconds": value})

    def test_scheduled_context_needs_its_own_opt_in(self):
        self.db.save_settings({"workflow_memory_enabled": True})
        run = self.engine.start(workflow(), scheduled=True)
        self.assertEqual(run["memory_export"]["reason"], "scheduled_records_disabled")
        self.assertEqual(self.binds, [])
        self.engine.cancel(run["run_id"])
        self.db.save_settings({"workflow_memory_allow_scheduled": True})
        run = self.engine.start(workflow(), scheduled=True)
        self.assertTrue(run["memory_export"]["writer_context"]["scheduled"])

    def test_provider_unavailability_is_visible_and_does_not_fail_the_run(self):
        self.db.save_settings({"workflow_memory_enabled": True})
        self.unavailable = "not_consented"
        run = self.engine.start(workflow())
        self.drive(run["run_id"])
        self.assertEqual(self.db.get_run(run["run_id"])["status"], "completed")
        self.assertEqual(self.service.status()["last_delivery"]["reason"], "not_consented")
        self.assertEqual(self.writes, [])

    def test_policy_disabled_before_dispatch_does_not_backfill_on_reenable(self):
        self.db.save_settings(
            {"workflow_memory_enabled": True, "workflow_memory_write_node_events": True}
        )
        run = self.engine.start(workflow())
        self.db.save_settings({"workflow_memory_enabled": False})
        asyncio.run(self.service.dispatch())
        self.db.save_settings({"workflow_memory_enabled": True})
        asyncio.run(self.service.dispatch())
        self.assertEqual(self.writes, [])
        self.assertEqual(self.service.outbox.entries(run["run_id"])[0]["status"], "unavailable")

    def test_runtime_dispatches_off_the_driver_and_api_reports_queued_not_stored(self):
        self.db.save_settings(
            {"workflow_memory_enabled": True, "workflow_memory_write_node_events": True}
        )
        runtime = Runtime(self.db, SpecStore([self.home / "workflows/global"]))
        runtime.memory.sink_factory = self.factory
        self.addCleanup(runtime.close)
        run = runtime.engine.start(workflow())
        runtime.start()
        self.assertTrue(self.delivered.wait(8))
        deadline = time.monotonic() + 8
        while not all(
            row["status"] == "queued" for row in runtime.memory.outbox.entries(run["run_id"])
        ):
            if time.monotonic() >= deadline:
                self.fail("accepted records did not settle to queued receipts")
            threading.Event().wait(0.02)
        self.assertEqual(self.db.get_run(run["run_id"])["status"], "completed")
        api = API(runtime)
        status, response = api.handle("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertEqual(response["memory"]["last_delivery"]["status"], "queued")
        status, records = api.handle("GET", f"/runs/{run['run_id']}/memory")
        self.assertEqual(status, 200)
        self.assertEqual(len(records["effects"]), 4)
        effect_id = records["effects"][0]["effect_id"]
        status, result = api.handle("POST", f"/runs/{run['run_id']}/memory/{effect_id}/reconcile")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(self.writes), 4)
        self.assertEqual(len(self.lookups), 1)
        self.assertNotIn("writer_context", json.dumps(records["effects"]))

    def test_all_write_flags_off_is_a_noop_and_status_never_binds(self):
        config = {
            "workflow_memory_enabled": True,
            "workflow_memory_write_run_summaries": False,
            "workflow_memory_write_node_failures": False,
            "workflow_memory_write_node_events": False,
        }
        self.db.save_settings(config)
        self.assertIsNone(policy(workflow(), config))
        run = self.engine.start(workflow())
        self.assertNotIn("memory_export", run)
        self.service.status()
        self.assertEqual(self.binds, [])


if __name__ == "__main__":
    unittest.main()
