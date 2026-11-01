from __future__ import annotations

import asyncio
import contextvars
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.api import API
from hermes_workflows.db import Database
from hermes_workflows.engine import Engine
from hermes_workflows.native import NativeHost
from hermes_workflows.notifications import (
    Notifications,
    notice_text,
    parse_origin,
    resolve_target,
    result_output,
)
from hermes_workflows.origin import NativeSender, Origins, build_origin
from hermes_workflows.runtime import Runtime
from hermes_workflows.specs import SpecStore, compile_preview, validate


def workflow():
    return {
        "id": "delivery",
        "name": "Delivery test",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "nodes": [{"id": "gate", "type": "human_review"}, {"id": "done", "type": "finish"}],
        "edges": [{"from": "gate", "to": "done"}],
    }


class Sender:
    def __init__(self, outcome=True):
        self.calls, self.ready, self.outcome = [], True, outcome

    def available(self):
        return self.ready

    async def send(self, target, text, identity, **kwargs):
        self.calls.append((target, text, identity))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow notices żółć ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = Database(self.home / "workflows/runs.db")
        self.engine = Engine(self.db)
        self.sender = Sender()
        self.notices = Notifications(self.db, self.home, self.sender)

    async def waiting(self, wf=None, origin="telegram:chat:thread"):
        wf = wf or workflow()
        run = self.engine.start(wf, origin=origin)
        self.assertTrue(self.db.claim(run["run_id"], "driver"))
        await self.engine.drive(run["run_id"], "driver")
        return self.db.get_run(run["run_id"]), wf

    async def finish(self, run):
        self.engine.review(run["run_id"], "gate", "approved")
        self.assertTrue(self.db.claim(run["run_id"], "driver2"))
        await self.engine.drive(run["run_id"], "driver2")

    async def test_waiting_completion_normal_polling_deduplicates(self):
        run, _ = await self.waiting()
        for _ in range(3):
            self.db.mutate_run(run["run_id"], lambda r: None)
            await self.notices.dispatch()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertIn("/workflow review", self.sender.calls[0][1])
        self.assertNotIn("Reply in this chat", self.sender.calls[0][1])
        await self.finish(run)
        await self.notices.dispatch()
        await self.notices.dispatch()
        self.assertEqual(len(self.sender.calls), 2)
        self.assertTrue(all(n["status"] == "sent" for n in self.notices.records(run["run_id"])))

    async def test_headless_notice_survives_restart_and_resolved_gate_is_superseded(self):
        self.sender.ready = False
        run, _ = await self.waiting()
        await self.notices.dispatch()
        self.assertEqual(self.notices.records(run["run_id"])[0]["attempts"], 0)
        await self.finish(run)
        restarted = Notifications(Database(self.db.path), self.home, self.sender)
        self.sender.ready = True
        await restarted.dispatch()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertIn("completed", self.sender.calls[0][1])
        self.assertEqual(
            {r["status"] for r in restarted.records(run["run_id"])}, {"sent", "superseded"}
        )

    async def test_result_target_silent_and_case_sensitive_marker(self):
        for output, expected in [
            ("[SILENT] no ping", "suppressed"),
            ("[silent] result", "sent"),
            ("result żółć", "sent"),
        ]:
            with self.subTest(output=output):
                wf = workflow()
                wf["deliver"] = "discord:destination"
                wf["id"] += str(time.time_ns())
                run, _ = await self.waiting(wf)
                self.db.mutate_run(
                    run["run_id"], lambda r, output=output: r["nodes"]["gate"].update(output=output)
                )
                await self.finish(run)
                await self.notices.dispatch()
                record = next(
                    n
                    for n in self.notices.records(run["run_id"])
                    if n["payload"]["event"] == "completed"
                )
                self.assertEqual(record["status"], expected)
                self.assertEqual(record["payload"]["text"], output)
                self.assertEqual(record["payload"]["target"], "discord:destination")

    async def test_default_snapshot_no_target_and_no_retroactive_history(self):
        self.db.save_settings({"default_deliver": "local"})
        run, _ = await self.waiting(origin=None)
        self.db.save_settings({"default_deliver": "telegram:changed"})
        self.assertEqual(self.notices.records(run["run_id"])[0]["payload"]["target"], "local")
        self.engine.cancel(run["run_id"])
        self.db.save_settings({"default_deliver": ""})
        with patch.dict(os.environ, {"HERMES_WORKFLOWS_DELIVER": ""}):
            run, _ = await self.waiting(origin=None)
        self.assertEqual(self.notices.records(run["run_id"])[0]["status"], "suppressed")
        self.db.mutate_run(run["run_id"], lambda r: r.pop("notification_context"))
        await self.finish(run)
        self.assertEqual(len(self.notices.records(run["run_id"])), 1)

    async def test_retry_and_cancel_fence_pending_notices(self):
        run, wf = await self.waiting()
        old_attempt = run["nodes"]["gate"]["attempt_id"]
        self.engine.retry(run["run_id"], wf)
        self.assertTrue(self.db.claim(run["run_id"], "new"))
        await self.engine.drive(run["run_id"], "new")
        current = self.db.get_run(run["run_id"])
        self.assertNotEqual(current["nodes"]["gate"]["attempt_id"], old_attempt)
        records = self.notices.records(run["run_id"])
        self.assertEqual({n["status"] for n in records}, {"pending", "superseded"})
        self.engine.cancel(run["run_id"])
        await self.notices.dispatch()
        self.assertEqual(self.sender.calls, [])

    async def test_sender_failure_is_unknown_redacted_and_never_replayed(self):
        run, _ = await self.waiting()
        self.sender.outcome = RuntimeError("SECRET should not be recorded")
        await self.notices.dispatch()
        self.sender.outcome = True
        await self.notices.dispatch()
        records = self.notices.records(run["run_id"])
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(records[0]["status"], "unknown")
        self.assertNotIn("SECRET", json.dumps(records))
        self.assertEqual(self.db.get_run(run["run_id"])["status"], "waiting")
        self.notices.reconcile(records[0]["notice_id"], delivered=True)
        self.assertEqual(self.notices.records(run["run_id"])[0]["status"], "sent")
        with self.assertRaises(ValueError):
            self.notices.reconcile(records[0]["notice_id"], delivered=False)

    async def test_lost_acknowledgement_and_profile_mismatch(self):
        run, _ = await self.waiting()
        with self.db.connect(write=True) as con:
            con.execute("UPDATE py_notifications SET status='in_flight',lease_until=0,owner='dead'")
        await self.notices.dispatch()
        self.assertEqual(self.notices.records(run["run_id"])[0]["status"], "unknown")
        self.assertEqual(self.sender.calls, [])
        self.engine.cancel(run["run_id"])
        run, _ = await self.waiting()
        wrong = Notifications(self.db, self.home / "other", self.sender)
        await wrong.dispatch()
        self.assertEqual(wrong.records(run["run_id"])[0]["error"], "profile_home_mismatch")

    async def test_real_process_death_after_send_is_not_replayed(self):
        run, _ = await self.waiting()
        proof = self.home / "sent.json"
        code = """import asyncio,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from hermes_workflows.db import Database
from hermes_workflows.notifications import Notifications
class Sender:
 def available(self):return True
 async def send(self,*args,**kwargs):
  Path(sys.argv[3]).write_text(args[2]);os._exit(19)
asyncio.run(Notifications(Database(Path(sys.argv[2])),Path(sys.argv[2]).parent.parent,Sender()).dispatch())
"""
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-S",
                "-c",
                code,
                str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"),
                str(self.db.path),
                str(proof),
            ],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertTrue(proof.read_text())
        with self.db.connect(write=True) as con:
            con.execute("UPDATE py_notifications SET lease_until=0")
        await self.notices.dispatch()
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.notices.records(run["run_id"])[0]["status"], "unknown")

    async def test_blocked_stuck_attention_and_pending_recovery(self):
        run = self.engine.start(workflow(), origin="discord:fixture")

        def blocked(r):
            r["status"] = "running"
            r["nodes"]["gate"].update(status="running", adoption={"blocked": ["t_a", "t_b"]})

        for _ in range(2):
            self.db.mutate_run(run["run_id"], blocked)
        self.assertEqual(len(self.notices.records(run["run_id"])), 2)
        await self.notices.dispatch()
        self.assertEqual(len(self.sender.calls), 2)

        def stuck(r):
            r["status"] = "failed"
            r["nodes"]["gate"].update(
                status="failed",
                error="adopt stuck: card t_a has at least 3 consecutive dispatch failures",
            )

        self.db.mutate_run(run["run_id"], stuck)
        await self.notices.dispatch()
        self.assertEqual(
            {n["payload"]["event"] for n in self.notices.records(run["run_id"])},
            {"blocked", "stuck", "failed"},
        )

    async def test_concurrent_dispatch_and_cancellation(self):
        run, _ = await self.waiting()
        admitted, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            self.sender.calls.append(args)
            admitted.set()
            await release.wait()
            return True

        self.sender.send = send
        task = asyncio.create_task(self.notices.dispatch())
        await admitted.wait()
        await Notifications(self.db, self.home, self.sender).dispatch()
        self.assertEqual(len(self.sender.calls), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.notices.records(run["run_id"])[0]["status"], "unknown")

    async def test_outbox_insert_and_state_update_roll_back_together(self):
        run = self.engine.start(workflow(), origin="local:chat")
        with patch(
            "hermes_workflows.notifications.notice_text",
            side_effect=RuntimeError("crash before commit"),
        ):
            with self.assertRaises(RuntimeError):
                self.db.mutate_run(run["run_id"], lambda r: r.update(status="completed"))
        self.assertEqual(self.db.get_run(run["run_id"])["status"], "created")
        self.assertEqual(self.notices.records(run["run_id"]), [])

    async def test_api_does_not_accept_spoofed_origin_and_exposes_reconciliation(self):
        store = SpecStore([self.home / "workflows/global"])
        store.save({"workflow": workflow()}, create=True)
        runtime = Runtime(self.db, store)
        self.addCleanup(runtime.close)
        with patch.object(runtime, "start", return_value=runtime):
            code, created = API(runtime).handle(
                "POST", "/workflows/delivery/run", {"origin": "telegram:spoof"}
            )
        self.assertEqual(code, 200)
        self.assertNotIn("origin", self.db.get_run(created["run_id"]))
        self.engine.cancel(created["run_id"])
        run, _ = await self.waiting()
        self.sender.outcome = False
        await self.notices.dispatch()
        code, data = API(runtime).handle("GET", f"/runs/{run['run_id']}/notifications")
        self.assertEqual(code, 200)
        identity = data["notifications"][0]["notice_id"]
        code, _ = API(runtime).handle(
            "POST",
            f"/runs/{run['run_id']}/notifications/{identity}/reconcile",
            {"delivered": False},
        )
        self.assertEqual(code, 200)


class OriginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.selected = contextvars.ContextVar("notice-home", default=self.home)
        self.constants = SimpleNamespace(
            get_hermes_home=self.selected.get,
            set_hermes_home_override=self.selected.set,
            reset_hermes_home_override=self.selected.reset,
        )

        class Target:
            def __init__(self, value):
                self.value = value

            @classmethod
            def parse(cls, value):
                return cls(value)

            def to_string(self):
                return self.value

        self.delivery = SimpleNamespace(DeliveryTarget=Target, DeliveryRouter=object)
        self.host = NativeHost(
            self.home, loader=lambda n: self.constants if n == "hermes_constants" else self.delivery
        )
        self.origins = Origins(self.host)
        self.source = SimpleNamespace(
            platform=SimpleNamespace(value="telegram"), chat_id="chat", thread_id="thread"
        )
        self.event = SimpleNamespace(source=self.source)
        self.calls = []

        async def deliver(text, targets, **kwargs):
            self.calls.append(
                (text, targets[0].to_string(), self.selected.get(), threading.get_ident())
            )
            return {targets[0].to_string(): {"success": True}}

        self.gateway = SimpleNamespace(
            delivery_router=SimpleNamespace(deliver=deliver, output_dir=self.home / "cron/output"),
            _session_key_for_source=lambda s: "session",
            _authorization_home_for_source=lambda s: None,
        )

    async def test_capture_profile_scope_bounded_cache_and_sender(self):
        await self.origins.capture(self.event, self.gateway)
        self.assertEqual(self.origins.origin_for("session"), "telegram:chat:thread")
        self.assertEqual(build_origin(self.source), "telegram:chat:thread")
        sender = NativeSender(self.origins)
        self.assertTrue(sender.available())
        self.assertTrue(await sender.send("discord:target", "fixture", "id"))
        self.assertEqual(self.calls[0][2], self.home)
        self.selected.set(self.home / "wrong")
        await self.origins.capture(
            SimpleNamespace(source=SimpleNamespace(platform="discord", chat_id="bad")), self.gateway
        )
        self.assertEqual(self.origins.origin_for("session"), "telegram:chat:thread")
        self.origins.clear()
        self.assertFalse(sender.available())

    async def test_cross_thread_router_is_awaited_on_owner_loop_and_nested_failure_checked(self):
        await self.origins.capture(self.event, self.gateway)
        sender = NativeSender(self.origins)
        owner = threading.get_ident()
        result = await asyncio.to_thread(lambda: asyncio.run(sender.send("local", "proof", "id")))
        self.assertTrue(result)
        self.assertEqual(self.calls[0][3], owner)

        async def failed(*args, **kwargs):
            return {"local": {"success": True, "result": {"success": False}}}

        self.gateway.delivery_router.deliver = failed
        self.assertFalse(await sender.send("local", "proof", "id2"))

    async def test_authority_and_router_home_mismatch_are_rejected(self):
        self.gateway._authorization_home_for_source = lambda s: self.home / "other"
        await self.origins.capture(self.event, self.gateway)
        self.assertIsNone(self.origins.binding)
        self.gateway._authorization_home_for_source = lambda s: self.home
        await self.origins.capture(self.event, self.gateway)
        self.gateway.delivery_router.output_dir = self.home / "other/cron/output"
        self.assertFalse(NativeSender(self.origins).available())

    async def test_router_replacement_does_not_change_an_admitted_send(self):
        await self.origins.capture(self.event, self.gateway)
        validated = self.gateway.delivery_router
        foreign_calls = []

        async def foreign_send(*args, **kwargs):
            foreign_calls.append(args)
            return {"local": {"success": True}}

        foreign = SimpleNamespace(output_dir=self.home / "other/cron/output", deliver=foreign_send)

        class RebindingGateway:
            reads = 0

            @property
            def delivery_router(self):
                self.reads += 1
                return validated if self.reads == 1 else foreign

        gateway = RebindingGateway()
        self.origins.binding = (gateway, asyncio.get_running_loop())
        self.assertTrue(await NativeSender(self.origins).send("local", "proof", "id"))
        self.assertEqual(foreign_calls, [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(gateway.reads, 1)


class ContractTests(unittest.TestCase):
    def test_frozen_upstream_reference_decisions(self):
        reference = json.loads(
            (Path(__file__).parent / "fixtures/notification-reference.json").read_text(
                encoding="utf-8"
            )
        )
        for case in reference["targets"]:
            self.assertEqual(
                resolve_target(case["origin"], case["default"], case["deliver"]), case["expected"]
            )
        for case in reference["silent"]:
            self.assertEqual("[SILENT]" in (case["text"] or ""), case["expected"])
        for case in reference["outputs"]:
            self.assertEqual(result_output({"nodes": case["nodes"]}), case["expected"])

    def test_target_policy_and_schema(self):
        for origin, default, deliver, expected in [
            ("telegram:a", "local", None, "telegram:a"),
            (None, "local", "origin", "local"),
            ("telegram:a", "local", "discord:b", "discord:b"),
            (None, None, "origin", None),
        ]:
            self.assertEqual(resolve_target(origin, default, deliver), expected)
        self.assertEqual(
            parse_origin("discord:chat:thread:colon"), ("discord", "chat", "thread:colon")
        )
        self.assertIsNone(parse_origin("bad"))
        wf = workflow()
        wf.update(deliver="origin", notifications={"subscribe_cards": False})
        self.assertTrue(validate(wf)["valid"])
        self.assertFalse(compile_preview(wf)["subscribe_cards"])
        self.assertEqual(compile_preview(wf)["deliver"], "origin")
        for field, value in [("deliver", 12), ("notifications", {"subscribe_cards": "no"})]:
            invalid = {**wf, field: value}
            self.assertFalse(validate(invalid)["valid"])
        self.assertIn(
            "review gate", notice_text({"workflow_id": "wf", "run_id": "r"}, "waiting", "gate")
        )
