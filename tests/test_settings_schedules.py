from __future__ import annotations

import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-workflows-py"
sys.path.insert(0, str(PLUGIN))
from hermes_workflows import settings
from hermes_workflows.api import API
from hermes_workflows.db import ConflictError, Database
from hermes_workflows.native import NativeUnavailable
from hermes_workflows.native_schedules import Schedules, fire
from hermes_workflows.runtime import Runtime
from hermes_workflows.schedules import Cron
from hermes_workflows.specs import SpecStore


def workflow():
    return {
        "id": "schedule-qa",
        "name": "Schedule QA",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "cron", "schedule": "*/5 * * * *", "timezone": "UTC"},
        "nodes": [{"id": "done", "type": "finish"}],
        "edges": [],
    }


class ConfigHost:
    def __init__(self):
        self.config = {
            "model": {"key": "${UNRELATED_ENV_REF}"},
            "plugins": {"enabled": ["another"], "other": {"keep": True}},
        }
        self.patches = []
        self.fail = False

    def env_settings(self, names):
        import os

        return {key: os.environ.get(key) for key in names}

    def call(self, feature, operation, *args, **kwargs):
        assert feature == "config"
        if operation == "load_config":
            return copy.deepcopy(self.config)
        assert operation == "save_config" and kwargs == {"merge_existing": True}
        if self.fail:
            raise NativeUnavailable("capture-only write failure")
        delta = args[0]
        assert set(delta) == {"plugins"} and set(delta["plugins"]) == {"workflows"}
        self.patches.append(copy.deepcopy(delta))
        self.config["plugins"].setdefault("workflows", {}).update(delta["plugins"]["workflows"])


class FakeJobs:
    def __init__(self):
        self.jobs = {}
        self.creates = 0
        self.fail_update = False

    def create_job(self, prompt, schedule, **kwargs):
        assert prompt is None and kwargs["no_agent"] and kwargs["paused"]
        self.creates += 1
        identity = "native-" + str(self.creates)
        self.jobs[identity] = {
            "id": identity,
            "enabled": False,
            "state": "paused",
            "next_run_at": Cron(schedule).next().isoformat(),
            "schedule": {"expr": schedule},
            **kwargs,
        }
        return self.get_job(identity)

    def get_job(self, identity):
        return copy.deepcopy(self.jobs.get(identity))

    def list_jobs(self, include_disabled=False):
        return [copy.deepcopy(j) for j in self.jobs.values() if include_disabled or j["enabled"]]

    def update_job(self, identity, changes):
        if self.fail_update:
            raise NativeUnavailable("capture-only update failure")
        if "schedule" in changes:
            changes = {**changes, "schedule": {"expr": changes["schedule"]}}
        self.jobs[identity].update(changes)
        return self.get_job(identity)

    def pause_job(self, identity):
        self.jobs[identity].update(enabled=False, state="paused")
        return self.get_job(identity)

    def resume_job(self, identity):
        self.jobs[identity].update(enabled=True, state="scheduled")
        return self.get_job(identity)

    def trigger_job(self, identity):
        self.jobs[identity].update(
            enabled=True, state="scheduled", next_run_at=datetime.now(timezone.utc).isoformat()
        )
        return self.get_job(identity)

    def remove_job(self, identity):
        return self.jobs.pop(identity, None) is not None

    def use_cron_store(self, home):
        self.home = home
        return nullcontext()


class CronHost:
    def __init__(self):
        self.jobs = FakeJobs()
        self.available = True

    def scope(self):
        return nullcontext()

    def module(self, feature):
        assert feature == "cron"
        if not self.available:
            raise NativeUnavailable("capture-only missing host")
        return self.jobs


class SettingsSchedulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow schedule żółć ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.db = Database(self.home / "workflows/runs.db")
        self.specs = SpecStore([self.home / "workflows/global"])
        self.runtime = Runtime(self.db, self.specs)
        self.addCleanup(self.runtime.close)
        self.host = CronHost()
        self.schedules = Schedules(self.runtime, host=self.host)
        self.runtime.schedules = self.schedules
        self.wf = workflow()
        self.specs.save({"workflow": self.wf}, create=True)

    def native(self):
        self.db.save_settings({"scheduler_backend": "native"})
        self.schedules.sync(self.wf)
        return self.db.schedules()[0]

    def test_config_legacy_env_default_and_partial_namespace_migration(self):
        host = ConfigHost()
        store = settings.Settings(self.db, host=host)
        self.db.save_settings(
            {"scripts_enabled": True, "trace_enabled": False, "default_timeout_seconds": 900}
        )
        with patch.dict(
            "os.environ", {"HERMES_WORKFLOWS_TRACE": "true", "HERMES_WORKFLOWS_BOARD": "env-board"}
        ):
            effective = store.values()
            self.assertFalse(effective["trace_enabled"])
            self.assertEqual(effective["internal_board"], "env-board")
            host.config["plugins"]["workflows"] = {"scripts_enabled": False, "trace_enabled": True}
            self.assertFalse(store.values()["scripts_enabled"])
            self.assertTrue(store.values()["trace_enabled"])
            store.save({"trace_enabled": "false", "max_parallel_runs": "7"})
        stored = host.config["plugins"]["workflows"]
        self.assertNotIn("default_timeout_seconds", stored)
        self.assertEqual(store.values()["default_timeout_seconds"], 900)
        self.assertFalse(stored["scripts_enabled"])
        self.assertNotIn("internal_board", stored)  # no default/env materialization
        self.assertEqual(host.config["plugins"]["enabled"], ["another"])
        self.assertEqual(host.config["model"]["key"], "${UNRELATED_ENV_REF}")
        self.assertTrue(host.config["plugins"]["other"]["keep"])
        host.fail = True
        with self.assertRaises(NativeUnavailable):
            store.save({"scripts_enabled": True})
        self.assertFalse(store.values()["scripts_enabled"])

    def test_schema_reference_types_paths_and_enforcement_are_honest(self):
        fields = {f["key"]: f for g in settings.schema(self.db)["groups"] for f in g["fields"]}
        reference = json.loads(
            (Path(__file__).parent / "fixtures/settings-reference.json").read_text()
        )
        for key, expected in reference["fields"].items():
            self.assertEqual(fields[key]["type"], expected["type"], key)
            if "options" in expected:
                self.assertEqual(fields[key]["options"], expected["options"], key)
        self.assertEqual(fields["runs_db_path"]["default"], str(self.db.path))
        for key in ("mode", "write_node_events", "internal_board", "tick_schedule"):
            self.assertFalse(fields[key]["enforced"])
        self.assertTrue(fields["scheduler_backend"]["enforced"])
        self.assertTrue(fields["default_mode"]["enforced"])
        for invalid in (
            {"max_parallel_runs": True},
            {"max_parallel_runs": 129},
            {"mode": "bad"},
            {"scripts_enabled": "bad"},
            {"unknown": 1},
            {"dashboard_api_port": 65536},
        ):
            with self.assertRaises(ValueError):
                settings.validate(invalid)

    def test_native_config_writers_patch_only_their_edits_and_detect_readonly_host(self):
        host = ConfigHost()
        self.db.save_settings({"trace_enabled": False, "scripts_enabled": False})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(settings.Settings(self.db, host=host).save, patch)
                for patch in ({"trace_enabled": True}, {"max_parallel_runs": 7})
            ]
            for future in futures:
                future.result()
        self.assertTrue(host.config["plugins"]["workflows"]["trace_enabled"])
        self.assertEqual(host.config["plugins"]["workflows"]["max_parallel_runs"], 7)
        self.assertNotIn("scripts_enabled", host.config["plugins"]["workflows"])
        load = host.call
        with patch.object(
            host,
            "call",
            side_effect=lambda f, op, *a, **k: (
                load(f, op, *a, **k) if op == "load_config" else None
            ),
        ):
            with self.assertRaises(NativeUnavailable):
                settings.Settings(self.db, host=host).save({"trace_enabled": False})

    def test_native_handoff_blocks_legacy_writers_from_reactivating_python_firing(self):
        row = self.native()
        row["next_run"] = datetime.now(timezone.utc).isoformat()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.put_schedule(row)
        for key in ("authority", "generation", "native_id"):
            row.pop(key)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.put_schedule(row)
        self.assertIsNone(self.db.schedules()[0]["next_run"])
        self.assertEqual(len(self.schedules.rows()), 1)

    def test_schema4_upgrade_preserves_schedules_and_run_snapshots(self):
        self.db.save_settings({"scheduler_backend": "standalone"})
        self.schedules.sync(self.wf)
        run = self.runtime.engine.start(self.wf)
        before = self.db.schedules()
        with self.db.connect(write=True) as con:
            con.execute("DROP TRIGGER py_schedule_authority_guard")
            con.execute("UPDATE py_metadata SET version=4")
        upgraded = Database(self.db.path)
        self.assertEqual(upgraded.get_run(run["run_id"]), run)
        self.assertEqual(upgraded.schedules(), before)
        self.assertEqual(upgraded.settings()["scheduler_backend"], "standalone")
        with upgraded.connect() as con:
            self.assertIsNotNone(
                con.execute(
                    "SELECT name FROM sqlite_master WHERE name='py_schedule_authority_guard'"
                ).fetchone()
            )

    def test_invalid_env_does_not_enable_scripts_or_crash_defaults(self):
        with patch.dict("os.environ", {"HERMES_WORKFLOWS_TRACE": "invalid"}):
            with self.assertLogs("hermes_workflows.settings", level="WARNING"):
                self.assertFalse(settings.values(self.db)["trace_enabled"])
        self.assertFalse(settings.values(self.db)["scripts_enabled"])

    def test_local_to_native_is_single_and_preserves_saved_due_pause_and_edits(self):
        self.db.save_settings({"scheduler_backend": "standalone"})
        self.schedules.sync(self.wf)
        row = self.db.schedules()[0]
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        row.update(
            next_run=due, cron_expression="*/10 * * * *", last_run="2026-01-01T00:00:00+00:00"
        )
        self.db.put_schedule(row)
        self.db.save_settings({"scheduler_backend": "native"})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.schedules.sync(self.wf), range(4)))
        saved = self.db.schedules()[0]
        self.assertIsNone(saved["next_run"])
        self.assertEqual(saved["authority"], "native")
        self.assertEqual(self.host.jobs.creates, 1)
        job = self.host.jobs.get_job(saved["native_id"])
        self.assertEqual(job["next_run_at"], due)
        self.assertEqual(job["schedule"]["expr"], "*/10 * * * *")
        self.assertTrue(job["enabled"])
        self.runtime.tick_schedules()
        self.assertEqual(self.db.list_runs(), [])
        self.assertEqual(saved["last_run"], row["last_run"])
        self.schedules.script(self.wf["id"]).unlink()
        self.schedules.sync(self.wf)
        script = self.schedules.script(self.wf["id"])
        self.assertTrue(script.exists())
        self.assertNotIn("bash", script.read_text())
        compile(script.read_text(), str(script), "exec")

    def test_crash_after_native_create_reconciles_paused_intent_without_duplicate(self):
        self.db.save_settings({"scheduler_backend": "native"})
        self.host.jobs.fail_update = True
        with self.assertRaises(NativeUnavailable):
            self.schedules.sync(self.wf)
        self.assertEqual(self.db.schedules(), [])
        self.assertEqual(self.host.jobs.creates, 1)
        self.assertFalse(self.host.jobs.list_jobs(True)[0]["enabled"])
        self.host.jobs.fail_update = False
        self.schedules.sync(self.wf)
        self.assertEqual(self.host.jobs.creates, 1)
        self.assertEqual(len(self.schedules.rows()), 1)

    def test_api_native_controls_foreign_rejection_and_host_edits_survive_restart(self):
        row = self.native()
        identity = row["native_id"]
        api = API(self.runtime)
        route = "/schedules/" + identity
        self.assertEqual(api.handle("POST", route + "/pause")[0], 200)
        self.assertFalse(self.host.jobs.get_job(identity)["enabled"])
        self.schedules.sync(self.wf)
        self.assertFalse(self.schedules.rows()[0]["enabled"])
        self.assertEqual(api.handle("POST", route + "/resume")[0], 200)
        self.assertEqual(api.handle("PUT", route, {"cron": "every 10m"})[0], 200)
        self.assertEqual(api.handle("POST", route + "/run")[0], 200)
        self.assertEqual(self.db.list_runs(), [])  # host owns actual firing
        self.host.jobs.update_job(identity, {"schedule": "every 15m", "enabled": False})
        self.schedules.sync(self.wf)
        self.assertFalse(self.schedules.rows()[0]["enabled"])
        self.assertEqual(self.schedules.rows()[0]["cron_expression"], "every 15m")
        self.assertEqual(api.handle("POST", "/schedules/unrelated/pause")[0], 404)
        self.host.jobs.jobs[identity]["script"] = str(self.home / "foreign.py")
        self.assertEqual(api.handle("POST", route + "/pause")[0], 409)

    def test_deletion_tombstones_and_manual_trigger_remove_only_owned_job(self):
        row = self.native()
        self.schedules.action(row["native_id"], "delete")
        self.schedules.sync(self.wf)
        self.assertEqual(self.host.jobs.creates, 1)
        self.assertEqual(self.schedules.rows(), [])
        self.wf["trigger"]["schedule"] = "0 * * * *"
        self.schedules.sync(self.wf)
        self.assertEqual(self.host.jobs.creates, 2)
        self.host.jobs.remove_job(self.db.schedules()[0]["native_id"])
        self.schedules.sync(self.wf)
        self.assertEqual(self.schedules.rows(), [])
        self.wf["trigger"] = {"type": "manual"}
        self.schedules.sync(self.wf)
        self.assertEqual(self.db.schedules(), [])

    def test_native_authority_does_not_fall_back_when_host_disappears(self):
        self.native()
        self.host.available = False
        self.db.save_settings({"scheduler_backend": "auto"})
        with self.assertRaises(NativeUnavailable):
            self.schedules.sync(self.wf)
        self.assertIsNone(self.db.schedules()[0]["next_run"])
        self.db.save_settings({"scheduler_backend": "standalone"})
        with self.assertRaises(NativeUnavailable):
            self.schedules.sync(self.wf)
        self.host.available = True
        self.schedules.sync(self.wf)
        saved = self.db.schedules()[0]
        self.assertEqual(saved["authority"], "standalone")
        self.assertIsNotNone(saved["next_run"])
        self.assertFalse(self.host.jobs.get_job(saved["native_id"])["enabled"])
        result = self.schedules.action(saved["hermes_cron_id"], "run")
        self.assertIn("run_id", result)
        self.assertIsNotNone(self.db.schedules()[0]["last_run"])

    def test_native_claim_fire_is_idempotent_after_settlement_and_fenced_on_handoff(self):
        row = self.native()
        self.host.jobs.jobs[row["native_id"]]["fire_claim"] = {"by": "qa:claim-1"}
        with (
            patch("hermes_workflows.native_schedules.NativeHost", return_value=self.host),
            redirect_stdout(io.StringIO()),
        ):
            fire(str(self.db.path), self.wf["id"])
            first = self.db.list_runs()[0]
            self.runtime.engine.cancel(first["run_id"])
            fire(str(self.db.path), self.wf["id"])
            self.assertEqual(len(self.db.list_runs("all")), 1)
            self.db.save_settings({"scheduler_backend": "standalone"})
            self.schedules.sync(self.wf)
            with self.assertRaises(ConflictError):
                fire(str(self.db.path), self.wf["id"])
        self.assertEqual(len(self.db.list_runs("all")), 1)

    def test_intervals_and_dst_skip_and_fold(self):
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(Cron("every 30s").next(when), when + timedelta(minutes=1))
        self.assertEqual(Cron("every 2h").next(when), when + timedelta(hours=2))
        try:
            spring = Cron("30 2 * * *", "America/New_York")
            fall = Cron("30 1 * * *", "America/New_York")
        except ValueError:
            self.skipTest("IANA timezone database is unavailable")
        self.assertEqual(
            spring.next(datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)),
            datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc),
        )
        first = fall.next(datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc))
        second = fall.next(first)
        self.assertEqual(second - first, timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
