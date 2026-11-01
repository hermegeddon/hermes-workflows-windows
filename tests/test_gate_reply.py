from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-workflows-py"
sys.path.insert(0, str(PLUGIN))
from hermes_workflows import _command
from hermes_workflows.db import Database
from hermes_workflows.gate_reply import ChatReviews, interpret_reply
from hermes_workflows.native import NativeHost
from hermes_workflows.notifications import notice_text
from hermes_workflows.origin import Origins
from hermes_workflows.runtime import Runtime
from hermes_workflows.specs import SpecStore


def workflow(name="chat-review", *, loop=False):
    return {
        "id": name,
        "name": name,
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "nodes": [
            {"id": "start", "type": "prompt", "prompt": "Review"},
            {"id": "gate", "type": "human_review"},
            {"id": "done", "type": "finish"},
        ],
        "edges": [
            {"from": "start", "to": "gate"},
            {
                "from": "gate",
                "to": "done",
                "condition": {"type": "review_status", "equals": "approved"},
            },
        ]
        + (
            [
                {
                    "from": "gate",
                    "to": "gate",
                    "condition": {"type": "review_status", "equals": "needs_changes"},
                }
            ]
            if loop
            else []
        ),
    }


class Sender:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure:
            raise self.failure
        return True


class GateReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chat reviews żółć ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.runtime = Runtime(
            Database(self.home / "workflows/runs.db"), SpecStore([self.home / "specs"])
        )
        self.addCleanup(self.runtime.close)
        self.selected = contextvars.ContextVar("review-home", default=self.home)
        self.pending = {
            "approval": False,
            "slash": False,
            "clarify": False,
            "busy": False,
            "update": False,
        }
        self.modules = {
            "hermes_constants": SimpleNamespace(get_hermes_home=self.selected.get),
            "tools.approval": SimpleNamespace(
                has_blocking_approval=lambda key: self.pending["approval"]
            ),
            "tools.slash_confirm": SimpleNamespace(get_pending=lambda key: self.pending["slash"]),
            "tools.clarify_gateway": SimpleNamespace(
                get_pending_for_session=lambda key, **kw: self.pending["clarify"]
            ),
        }
        self.runtime.origins = Origins(NativeHost(self.home, loader=self.modules.__getitem__))
        self.sender = Sender()
        self.runtime.notifications.sender = self.sender
        self.source = SimpleNamespace(
            platform="discord",
            chat_id="room",
            thread_id="thread",
            scope_id="guild",
            user_id="operator",
            chat_type="thread",
            is_bot=False,
        )
        self.gateway = SimpleNamespace(
            _session_key_for_source=lambda source: "session",
            _authorization_home_for_source=lambda source: None,
            _resolve_profile_home_for_source=lambda source: self.home,
            _is_user_authorized_for_source=lambda source: True,
            _is_session_running=lambda key: self.pending["busy"],
            _peek_session_state=lambda key: SimpleNamespace(
                persistent=SimpleNamespace(update_prompt_pending=self.pending["update"])
            ),
        )
        self.counter = 0

    def event(self, text="approved", **kw):
        self.counter += 1
        return SimpleNamespace(
            **{
                "text": text,
                "source": self.source,
                "timestamp": datetime.now(),
                "message_id": str(self.counter),
                **kw,
            }
        )

    async def waiting(self, wf=None, *, metadata=None, origin="discord:room:thread"):
        await self.runtime.origins.capture(self.event("/workflow run test"), self.gateway)
        run = self.runtime.engine.start(
            wf or workflow(),
            origin=origin,
            origin_metadata=metadata
            if metadata is not None
            else self.runtime.origins.routing_for("session"),
        )
        return await self.drive(run["run_id"])

    async def drive(self, run_id):
        self.assertTrue(self.runtime.db.claim(run_id, "test-driver"))
        await self.runtime.engine.drive(run_id, "test-driver")
        return self.runtime.db.get_run(run_id)

    async def route(self, event):
        return await self.runtime.pre_dispatch(event, self.gateway)

    async def test_explicit_decision_notes_and_frozen_reference(self):
        data = json.loads(
            (Path(__file__).parent / "fixtures/gate-reply-reference.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(data["reference"]["commit"], "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f")
        for case in data["interpretations"]:
            with self.subTest(case=case):
                self.assertEqual(list(interpret_reply(case["text"])), case["expected"])
        run = await self.waiting()
        result = await self.route(self.event("ApPrOvEd ship żółć"))
        self.assertEqual(result["action"], "skip")
        node = self.runtime.db.get_run(run["run_id"])["nodes"]["gate"]
        self.assertEqual((node["review_decision"], node["review_note"]), ("approved", "ship żółć"))
        self.assertEqual(self.sender.calls[0][0][0], "discord:room:thread")
        self.assertEqual(self.sender.calls[0][1]["metadata"]["thread_id"], "thread")
        self.assertEqual((await self.drive(run["run_id"]))["status"], "completed")

    def test_slash_command_uses_native_session_key_for_origin_capture(self):
        with patch("hermes_workflows._tool", return_value=(200, {"ok": True})) as tool:
            _command("run fixture", home=self.home, session_key="fixture-session")
        self.assertEqual(tool.call_args.kwargs["task_id"], "fixture-session")

    async def test_free_text_pick_preserves_full_note(self):
        run = await self.waiting()
        self.assertEqual((await self.route(self.event(" scope 3\nżółć ")))["action"], "skip")
        node = self.runtime.db.get_run(run["run_id"])["nodes"]["gate"]
        self.assertEqual(
            (node["review_decision"], node["review_note"]), ("approved", "scope 3\nżółć")
        )

    async def test_empty_slash_and_non_control_events_do_not_query_runs(self):
        for event in [
            self.event(""),
            self.event("  "),
            self.event(" /workflow list"),
            self.event(internal=True),
            self.event(allow_gateway_control=False),
            self.event(prompt_response={"prompt_id": "native"}),
            self.event(edit_revision="edit"),
            self.event("@file:x\n/approve", is_command=lambda: True),
        ]:
            with (
                self.subTest(event=event),
                patch.object(
                    self.runtime.db, "connect", side_effect=AssertionError("queried runs")
                ),
            ):
                self.assertIsNone(await self.route(event))
        self.assertEqual(self.sender.calls, [])

    async def test_ambiguous_runs_and_two_gates_in_one_run_fall_through(self):
        first = await self.waiting(workflow("first"))
        second = await self.waiting(workflow("second"))
        self.assertIsNone(await self.route(self.event()))
        self.runtime.engine.cancel(first["run_id"])
        self.runtime.engine.cancel(second["run_id"])
        wf = workflow("parallel")
        wf["nodes"] = [
            {"id": "start", "type": "prompt"},
            {"id": "a", "type": "human_review"},
            {"id": "b", "type": "human_review"},
        ]
        wf["edges"] = [{"from": "start", "to": "a"}, {"from": "start", "to": "b"}]
        run = await self.waiting(wf)
        self.assertIsNone(await self.route(self.event("3")))
        self.assertEqual(self.runtime.db.get_run(run["run_id"])["status"], "waiting")
        self.assertEqual(self.sender.calls, [])

    async def test_absent_gate_and_wrong_origin_session_scope_or_home_fall_through(self):
        self.assertIsNone(await self.route(self.event("ordinary chat")))
        run = await self.waiting()
        for field, value in [
            ("origin", "discord:other:thread"),
            ("session_key", "different"),
            ("scope_id", "other-guild"),
            ("home", self.home / "other"),
        ]:
            event = self.event()
            reply = self.runtime.chat_reviews._context(event, self.gateway, None)
            reply[field] = value
            self.assertEqual(self.runtime.chat_reviews.resolve(event.text, reply), (None, None))
        self.assertEqual(self.runtime.db.get_run(run["run_id"])["status"], "waiting")

    async def test_authorization_and_native_control_owners_take_precedence(self):
        run = await self.waiting()
        self.gateway._is_user_authorized_for_source = lambda source: False
        self.assertIsNone(await self.route(self.event()))
        self.gateway._is_user_authorized_for_source = lambda source: True
        for owner in self.pending:
            self.pending[owner] = True
            self.assertIsNone(await self.route(self.event("3")), owner)
            self.pending[owner] = False
        self.modules.pop("tools.approval")
        self.assertIsNone(await self.route(self.event()))
        self.assertEqual(self.runtime.db.get_run(run["run_id"])["status"], "waiting")

    async def test_profile_gateway_bot_and_missing_authorization_apis_are_refused(self):
        run = await self.waiting()
        self.selected.set(self.home / "foreign")
        self.assertIsNone(await self.route(self.event()))
        self.selected.set(self.home)
        self.gateway._authorization_home_for_source = lambda source: self.home / "foreign"
        self.assertIsNone(await self.route(self.event()))
        self.gateway._authorization_home_for_source = lambda source: None
        self.gateway._resolve_profile_home_for_source = lambda source: self.home / "foreign"
        self.assertIsNone(await self.route(self.event()))
        self.gateway._resolve_profile_home_for_source = lambda source: self.home
        other = copy.copy(self.gateway)
        self.assertIsNone(await self.runtime.chat_reviews.route(self.event(), other))
        self.source.is_bot = True
        self.assertIsNone(await self.route(self.event()))
        self.source.is_bot = False
        self.source.profile_route_rejected = True
        self.assertIsNone(await self.route(self.event()))
        self.source.profile_route_rejected = False
        del self.gateway._is_user_authorized_for_source
        self.assertIsNone(await self.route(self.event()))
        self.assertEqual(self.runtime.db.get_run(run["run_id"])["status"], "waiting")

    async def test_stale_time_and_quoted_old_attempt_do_not_resolve_new_iteration(self):
        run = await self.waiting(workflow(loop=True))
        old = run["nodes"]["gate"]["attempt_id"]
        stale = self.event(timestamp=datetime.now() - timedelta(seconds=5))
        self.assertEqual((await self.route(self.event("needs_changes repair")))["action"], "skip")
        new = await self.drive(run["run_id"])
        current = new["nodes"]["gate"]["attempt_id"]
        self.assertNotEqual(old, current)
        for event in [
            stale,
            self.event(reply_to_message_id="old", reply_to_text=f"[workflow-review:{old}]"),
            self.event(reply_to_message_id="unknown"),
            self.event(reply_to_text="ordinary old proposal"),
        ]:
            self.assertEqual((await self.route(event))["action"], "skip")
            self.assertNotIn(
                "review_decision", self.runtime.db.get_run(run["run_id"])["nodes"]["gate"]
            )
        good = self.event(
            reply_to_message_id="current", reply_to_text=f"[workflow-review:{current}]"
        )
        self.assertEqual((await self.route(good))["action"], "skip")
        self.assertEqual((await self.drive(run["run_id"]))["status"], "completed")

    async def test_replayed_message_is_consumed_after_restart_and_loop_reentry(self):
        run = await self.waiting(workflow(loop=True))
        event = self.event("needs_changes first note")
        await self.route(event)
        await self.drive(run["run_id"])
        self.runtime.chat_reviews = ChatReviews(self.runtime)
        self.runtime.db = Database(self.runtime.db.path)
        for _ in range(2):
            self.assertEqual((await self.route(event))["action"], "skip")
        state = self.runtime.db.get_run(run["run_id"])
        self.assertNotIn("review_decision", state["nodes"]["gate"])
        self.assertEqual(len(self.sender.calls), 1)

    async def test_concurrent_delivery_has_one_decision_receipt_and_confirmation(self):
        run = await self.waiting(workflow(loop=True))
        event = self.event("needs_changes once")
        results = await asyncio.gather(*(self.route(event) for _ in range(4)))
        self.assertTrue(all(result["action"] == "skip" for result in results))
        with self.runtime.db.connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM py_events WHERE json_extract(event,'$.chat_reply_id') IS NOT NULL"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(
            self.runtime.db.get_run(run["run_id"])["nodes"]["gate"]["review_note"], "once"
        )

    async def test_selection_rechecked_atomically_before_decision(self):
        first = await self.waiting(workflow("first"))
        event = self.event()
        reply = self.runtime.chat_reviews._context(event, self.gateway, None)
        original = self.runtime.engine.review

        def race(*args, **kwargs):
            second = self.runtime.engine.start(
                workflow("second"),
                origin=reply["origin"],
                origin_metadata=self.runtime.origins.routing_for("session"),
            )
            # Publish a second gate exactly between initial selection and mutation.
            self.runtime.db.mutate_run(
                second["run_id"],
                lambda r: (
                    r.update(status="waiting"),
                    r["nodes"]["gate"].update(status="waiting_for_review", started_at=time.time()),
                ),
            )
            return original(*args, **kwargs)

        with patch.object(self.runtime.engine, "review", race):
            self.assertEqual(self.runtime.chat_reviews.resolve(event.text, reply), (None, None))
        self.assertEqual(self.runtime.db.get_run(first["run_id"])["status"], "waiting")

    async def test_attempt_changed_between_selection_and_mutation_is_fenced(self):
        run = await self.waiting(workflow(loop=True))
        original = self.runtime.engine.review

        def race(*args, **kwargs):
            self.runtime.db.mutate_run(
                run["run_id"], lambda r: r["nodes"]["gate"].update(attempt_id="f" * 32)
            )
            return original(*args, **kwargs)

        with patch.object(self.runtime.engine, "review", race):
            self.assertEqual((await self.route(self.event()))["action"], "skip")
        self.assertNotIn("review_decision", self.runtime.db.get_run(run["run_id"])["nodes"]["gate"])
        self.assertIn("Could not resolve", self.sender.calls[-1][0][1])

    async def test_review_options_and_confirmation_errors_do_not_silently_approve(self):
        wf = workflow()
        wf["nodes"][1]["options"] = ["rejected"]
        wf["edges"] = [{"from": "start", "to": "gate"}, {"from": "gate", "to": "done"}]
        run = await self.waiting(wf)
        await self.route(self.event("3"))
        self.assertEqual(self.runtime.db.get_run(run["run_id"])["status"], "waiting")
        self.assertIn("Could not resolve", self.sender.calls[-1][0][1])
        self.sender.failure = RuntimeError("SECRET native failure")
        with self.assertLogs("hermes_workflows.gate_reply", level="WARNING") as logs:
            result = await self.route(self.event("Rejected reason"))
        self.assertEqual(result["action"], "skip")
        self.assertNotIn("SECRET", str(logs.output))
        self.assertEqual(
            self.runtime.db.get_run(run["run_id"])["nodes"]["gate"]["review_decision"], "rejected"
        )

    async def test_missing_legacy_route_or_timestamp_degrades_to_manual_review(self):
        run = await self.waiting(metadata={})
        self.assertIsNone(await self.route(self.event()))
        self.assertIsNone(await self.route(self.event(timestamp=None)))
        self.assertIsNone(
            await self.route(self.event(timestamp=datetime.now() + timedelta(minutes=1)))
        )
        self.assertEqual(self.runtime.db.get_run(run["run_id"])["status"], "waiting")

    async def test_waiting_notice_hints_only_for_bound_origin(self):
        run = await self.waiting()
        marker = f"[workflow-review:{run['nodes']['gate']['attempt_id']}]"
        self.assertIn(marker, notice_text(run, "waiting", "gate"))
        self.assertNotIn(marker, notice_text(run, "waiting", "gate", "discord:other"))
        self.assertNotIn(
            marker, notice_text({**run, "notification_context": {}}, "waiting", "gate")
        )

    async def test_unbound_gate_in_same_chat_prevents_guessing_a_new_bound_gate(self):
        await self.runtime.origins.capture(self.event(), self.gateway)
        routing = self.runtime.origins.routing_for("session")
        routing.pop("session_key")
        await self.waiting(workflow("legacy"), metadata=routing)
        run = await self.waiting(workflow("bound"))
        self.assertIsNone(await self.route(self.event("3")))
        self.assertEqual(self.runtime.db.get_run(run["run_id"])["status"], "waiting")

    async def test_review_receipt_rolls_back_with_decision_and_can_be_retried(self):
        run = await self.waiting()
        event = self.event("approved transactional")
        with patch("hermes_workflows.notifications.enqueue", side_effect=RuntimeError("SECRET")):
            self.assertEqual((await self.route(event))["action"], "skip")
        self.assertNotIn("SECRET", self.sender.calls[-1][0][1])
        self.assertEqual(self.runtime.db.get_run(run["run_id"])["status"], "waiting")
        with self.runtime.db.connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM py_events WHERE json_extract(event,'$.chat_reply_id') IS NOT NULL"
                ).fetchone()[0],
                0,
            )
        self.assertEqual((await self.route(event))["action"], "skip")
        self.assertEqual((await self.drive(run["run_id"]))["status"], "completed")

    async def test_confirmation_uses_admitted_thread_snapshot(self):
        await self.waiting()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.runtime.chat_reviews.resolve
        loop = asyncio.get_running_loop()

        def resolve(*args):
            result = original(*args)
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            return result

        with patch.object(self.runtime.chat_reviews, "resolve", resolve):
            task = asyncio.create_task(self.route(self.event()))
            await asyncio.wait_for(entered.wait(), 5)
            other = copy.copy(self.source)
            other.thread_id = "other-thread"
            await self.runtime.origins.capture(self.event(source=other), self.gateway)
            release.set()
            self.assertEqual((await task)["action"], "skip")
        self.assertEqual(self.sender.calls[0][0][0], "discord:room:thread")
        self.assertEqual(self.sender.calls[0][1]["metadata"]["thread_id"], "thread")

    async def test_process_death_after_review_commit_preserves_receipt(self):
        run = await self.waiting(workflow(loop=True))
        event = self.event("needs_changes committed")
        reply = self.runtime.chat_reviews._context(event, self.gateway, None)
        encoded = {**reply, "home": str(reply["home"])}
        code = """import json,os,sys
from pathlib import Path
from hermes_workflows.db import Database
from hermes_workflows.engine import Engine
reply=json.loads(sys.argv[3]);reply['home']=Path(reply['home'])
Engine(Database(sys.argv[1])).review(sys.argv[2],'gate','needs_changes','committed',attempt_id=sys.argv[4],_chat_reply=reply)
os._exit(23)
"""
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-S",
                "-c",
                "import sys;sys.path.insert(0,sys.argv.pop(1));" + code,
                str(PLUGIN),
                str(self.runtime.db.path),
                run["run_id"],
                json.dumps(encoded),
                run["nodes"]["gate"]["attempt_id"],
            ],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        await self.drive(run["run_id"])
        self.assertEqual((await self.route(event))["action"], "skip")
        self.assertEqual(self.sender.calls, [])
        self.assertNotIn("review_decision", self.runtime.db.get_run(run["run_id"])["nodes"]["gate"])
