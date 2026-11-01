from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.db import ConflictError, Database
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Executor
from hermes_workflows.kanban import KanbanExecutor
from test_kanban import BoardDouble, workflow


class NotifyBoard(BoardDouble):
    def __init__(self, root):
        super().__init__(root)
        self.subscriptions = set()
        self.fail = False

    def host(self, profile="default"):
        host = super().host(profile)
        load = host.loader
        host.loader = lambda name: self if name == "hermes_cli.kanban_db_notify" else load(name)
        return host

    def add_notify_sub(self, conn, **kwargs):
        if conn.in_transaction:
            raise AssertionError("native helper must own its transaction")
        if self.fail:
            raise RuntimeError("private credentials must never reach error state")
        self.subscriptions.add(tuple(sorted((k, str(v)) for k, v in kwargs.items())))


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kb = NotifyBoard(self.root / "native")
        self.db = Database(self.root / "workflows/runs.db")
        self.adapter = KanbanExecutor(self.db, self.kb.host("named"))
        self.engine = Engine(self.db, Executor(home=self.adapter.host.home))
        self.engine.kanban = self.adapter

    def seeded(self, wf, origin):
        run = self.engine.start(wf, origin=origin)
        self.assertTrue(self.db.claim(run["run_id"], "owner"))

        def begin(r):
            r["status"] = "running"
            r["nodes"]["work"].update(
                status="running",
                started_at=time.time(),
                executor="kanban",
                executor_home=str(self.adapter.host.home),
            )

        run = self.db.mutate_run(run["run_id"], begin, owner="owner")
        return run, {**wf["nodes"][0], "profile": "qa"}

    def test_policy_node_override_origin_and_native_idempotency(self):
        cases = [
            (None, None, "discord:chat:thread", True),
            (None, False, "discord:chat", False),
            (False, None, "discord:chat", False),
            (False, True, "discord:chat", True),
            (True, None, None, False),
            (True, None, "invalid", False),
        ]
        for number, (default, override, origin, expected) in enumerate(cases):
            with self.subTest(number=number):
                wf = workflow()
                wf["id"] += str(number)
                if default is not None:
                    wf["notifications"] = {"subscribe_cards": default}
                if override is not None:
                    wf["nodes"][0]["notify_completion"] = override
                run, node = self.seeded(wf, origin)
                before = len(self.kb.subscriptions)
                for _ in range(2):
                    self.adapter.schedule(
                        node,
                        run,
                        "demo",
                        owner="owner",
                        revision=0,
                        attempt_id=run["nodes"]["work"]["attempt_id"],
                        timeout=20,
                    )
                self.assertEqual(len(self.kb.subscriptions) - before, int(expected))
                if expected:
                    sub = dict(
                        next(
                            s
                            for s in self.kb.subscriptions
                            if dict(s)["task_id"]
                            == self.db.get_run(run["run_id"])["nodes"]["work"]["task_id"]
                        )
                    )
                    self.assertEqual(sub["notifier_profile"], "named")
                    self.assertEqual(sub["chat_id"], "chat")
                self.engine.cancel(run["run_id"])

    def test_subscription_failure_is_separate_from_card_execution(self):
        self.kb.fail = True
        run, node = self.seeded(workflow(), "discord:chat")
        handle = self.adapter.schedule(
            node,
            run,
            "demo",
            owner="owner",
            revision=0,
            attempt_id=run["nodes"]["work"]["attempt_id"],
            timeout=20,
        )
        state = self.db.get_run(run["run_id"])
        self.assertEqual(state["status"], "running")
        self.assertTrue(handle["task_id"])
        self.assertEqual(
            state["nodes"]["work"]["notification_errors"], ["card_subscription:RuntimeError"]
        )
        self.engine.cancel(run["run_id"])

    def test_cancelled_attempt_cannot_subscribe(self):
        run, node = self.seeded(workflow(), "discord:chat")
        self.engine.cancel(run["run_id"])
        with self.assertRaises(ConflictError):
            self.adapter.subscribe(
                node,
                run,
                "demo",
                "t_fake",
                owner="owner",
                revision=0,
                attempt_id=run["nodes"]["work"]["attempt_id"],
            )
        self.assertEqual(self.kb.subscriptions, set())
