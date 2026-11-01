from __future__ import annotations

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
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.db import Database
from hermes_workflows.effects import EffectStore
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Executor, Result
from hermes_workflows.kanban import CREATOR, KanbanExecutor, board_for, completion
from hermes_workflows.native import NativeHost
from hermes_workflows.runtime import Runtime
from hermes_workflows.specs import SpecStore, compile_preview, validate


class BoardDouble:
    """Real SQLite transactions, fake native workers; no host dependencies."""

    def __init__(self, root):
        self.root, self.boards, self.created = root, {}, []
        self.archived = []
        self.home = contextvars.ContextVar("kanban-test-home", default=None)
        self.constants = SimpleNamespace(
            set_hermes_home_override=self.home.set,
            reset_hermes_home_override=self.home.reset,
            get_hermes_home=self.home.get,
        )

    def host(self, profile="default"):
        def loader(name):
            if name == "hermes_constants":
                return self.constants
            if name in ("hermes_cli.kanban_db", "hermes_cli.kanban_db_connect"):
                return self
            raise ModuleNotFoundError(name)

        return NativeHost(self.root / "profiles" / profile, loader=loader)

    def kanban_home(self):
        return self.root

    def board_dir(self, slug):
        return self.root / "kanban/boards" / slug

    def kanban_db_path(self, *, board):
        return self.board_dir(board) / "kanban.db"

    def list_boards(self, **kwargs):
        return list(self.boards.values())

    def create_board(self, slug):
        self.board_dir(slug).mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.kanban_db_path(board=slug), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            self._schema(conn)
        finally:
            conn.close()
        self.boards[slug] = {"slug": slug, "archived": False}

    def init_db(self):
        pass

    def get_task(self):
        pass

    def claim_review_task(self):
        pass

    def connect(self, *, board):
        if board not in self.boards:
            raise ValueError("board missing")
        conn = sqlite3.connect(self.kanban_db_path(board=board), timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _schema(conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, created_by TEXT,
              idempotency_key TEXT, status TEXT, result TEXT, workflow_template_id TEXT,
              current_step_key TEXT, options TEXT);
            CREATE TABLE IF NOT EXISTS task_runs (
              id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, summary TEXT, metadata TEXT);
        """)

    @contextmanager
    def write_txn(self, conn, *, allow_nested=False):
        nested = conn.in_transaction
        if nested and not allow_nested:
            raise RuntimeError("nested")
        conn.execute("SAVEPOINT native_nested" if nested else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO native_nested" if nested else "ROLLBACK")
            if nested:
                conn.execute("RELEASE native_nested")
            raise
        else:
            conn.execute("RELEASE native_nested" if nested else "COMMIT")

    def create_task(self, conn, **kwargs):
        self.created.append((str(self.home.get()), kwargs))
        identity = "t_" + uuid.uuid4().hex[:8]
        with self.write_txn(conn, allow_nested=True):
            conn.execute(
                "INSERT INTO tasks(id,title,body,assignee,created_by,idempotency_key,status,options) VALUES(?,?,?,?,?,?,'ready',?)",
                (
                    identity,
                    kwargs["title"],
                    kwargs["body"],
                    kwargs["assignee"],
                    kwargs["created_by"],
                    kwargs["idempotency_key"],
                    json.dumps(kwargs),
                ),
            )
        return identity

    def archive_task(self, conn, task_id):
        self.archived.append(task_id)
        with self.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (task_id,))

    def cards(self, board="demo"):
        with closing(self.connect(board=board)) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM tasks")]

    def finish(
        self,
        task_id,
        *,
        board="demo",
        output="worker final after session rotation",
        outcome="completed",
        metadata=None,
    ):
        conn = self.connect(board=board)
        try:
            with self.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='done',result='old snapshot' WHERE id=?", (task_id,)
                )
                conn.execute(
                    "INSERT INTO task_runs(task_id,outcome,summary,metadata) VALUES(?,?,?,?)",
                    (task_id, outcome, output, json.dumps(metadata or {})),
                )
        finally:
            conn.close()


def workflow():
    return {
        "id": "board-test",
        "name": "Board test",
        "version": 1,
        "scope": {"type": "project", "projects": ["demo"]},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "qa"},
        "nodes": [
            {"id": "work", "type": "agent_task", "prompt": "Do the work"},
            {"id": "done", "type": "finish"},
        ],
        "edges": [{"from": "work", "to": "done"}],
    }


class KanbanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kanban workflow żółć ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "workflow/runs.db")
        self.kb = BoardDouble(self.root / "native")
        self.adapter = KanbanExecutor(self.db, self.kb.host(), poll_seconds=0.01)

    def runtime(self, runner=None):
        runtime = Runtime(self.db, SpecStore([self.root / "global"]), Executor(agent_runner=runner))
        runtime.engine.kanban = self.adapter
        self.addCleanup(runtime.close)
        return runtime.start()

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("condition did not settle")

    def wait_card(self, index=0):
        return self.wait(
            lambda: (
                self.kb.cards()[index]
                if "demo" in self.kb.boards and len(self.kb.cards()) > index
                else None
            )
        )

    def settled(self, run_id):
        return self.wait(
            lambda: (
                r
                if (r := self.db.get_run(run_id))["status"]
                in ("completed", "failed", "cancelled", "waiting")
                else None
            )
        )

    def seeded(self):
        wf = workflow()
        engine = Engine(self.db)
        run = engine.start(wf)
        self.db.claim(run["run_id"], "old")

        def begin(r):
            r["status"] = "running"
            r["nodes"]["work"].update(
                status="running",
                started_at=time.time(),
                executor="kanban",
                execution_timeout=30,
                executor_home=str(self.adapter.host.home),
            )

        run = self.db.mutate_run(run["run_id"], begin, owner="old")
        node = {**wf["nodes"][0], "profile": "qa"}
        return wf, run, node

    def test_route_and_preview_keep_global_and_explicit_off_board_direct(self):
        wf = workflow()
        self.assertEqual(board_for(wf), "demo")
        self.assertEqual(board_for(wf, "Other"), "other")
        engine = Engine(self.db)
        run = engine.start(wf)
        self.assertEqual(run["project_id"], "demo")
        engine.cancel(run["run_id"])
        run = engine.start(wf, project_id="Other")
        self.assertEqual(run["project_id"], "Other")
        self.assertEqual(run["board"], "other")
        engine.cancel(run["run_id"])
        self.assertNotIn("off_board", compile_preview(wf)["kanban_tasks"][0])
        wf["nodes"][0]["board"] = False
        self.assertTrue(compile_preview(wf)["kanban_tasks"][0]["off_board"])
        wf["nodes"][0]["board"] = "no"
        self.assertFalse(validate(wf)["valid"])
        with self.assertRaises(ValueError):
            board_for(workflow(), "../wrong")

    def test_project_worker_card_options_and_final_native_summary(self):
        runtime = self.runtime()
        wf = workflow()
        wf["nodes"][0].update(
            model="test-model", skills=[], workdir=str(self.root), max_retries=2, timeout_seconds=20
        )
        run = runtime.engine.start(wf)
        card = self.wait_card()
        self.assertEqual(
            (card["workflow_template_id"], card["current_step_key"], card["assignee"]),
            ("board-test", "work", "qa"),
        )
        options = json.loads(card["options"])
        self.assertEqual(options["skills"], [])
        self.assertEqual(options["model_override"], "test-model")
        self.assertEqual(options["workspace_kind"], "dir")
        self.assertEqual(options["workspace_path"], str(self.root))
        self.assertEqual(options["max_retries"], 2)
        self.assertEqual(options["max_runtime_seconds"], 20)
        self.kb.finish(card["id"])
        final = self.settled(run["run_id"])
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(final["nodes"]["work"]["output"], "worker final after session rotation")
        self.assertEqual(final["nodes"]["work"]["task_id"], card["id"])
        self.assertEqual(len(self.kb.created), 1)
        self.assertEqual(self.db.attempts(run["run_id"])[1]["handle"]["task_id"], card["id"])
        self.assertIsNone(self.kb.home.get())

    def test_direct_selection_does_not_load_or_create_a_board(self):
        calls = []

        async def runner(node, run):
            calls.append(run["run_id"])
            return Result(output="direct")

        runtime = self.runtime(runner)
        for scope, off in [
            ({"type": "global"}, True),
            ({"type": "project", "projects": ["demo"]}, False),
            ({"type": "project"}, True),
        ]:
            wf = workflow()
            wf["scope"] = scope
            wf["nodes"][0]["board"] = off
            result = self.settled(runtime.engine.start(wf)["run_id"])
            self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.kb.boards, {})

    def test_missing_native_host_fails_without_direct_fallback(self):
        async def forbidden(*args):
            raise AssertionError("must not fall back")

        runtime = self.runtime(forbidden)
        runtime.engine.kanban = KanbanExecutor(
            self.db, NativeHost(self.root, loader=lambda name: (_ for _ in ()).throw(ImportError()))
        )
        wf = workflow()
        wf["edges"][0]["condition"] = {"type": "node_status", "node": "work", "equals": "success"}
        final = self.settled(runtime.engine.start(wf)["run_id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("profile-scoped", final["nodes"]["work"]["error"])

    def test_acknowledgement_loss_reconciles_one_card_even_when_archived(self):
        wf, run, node = self.seeded()

        class Crash(BaseException):
            pass

        args = dict(
            owner="old", revision=0, attempt_id=run["nodes"]["work"]["attempt_id"], timeout=30
        )
        with patch.object(self.adapter.effects, "attach_handle", side_effect=Crash):
            with self.assertRaises(Crash):
                self.adapter.schedule(node, run, "demo", **args)
        card = self.kb.cards()[0]
        self.db.release(run["run_id"], "old")
        self.db.claim(run["run_id"], "new")
        EffectStore(self.db).recover(run["run_id"], owner="new", revision=0)
        handle = self.adapter.schedule(node, run, "demo", **{**args, "owner": "new"})
        self.assertEqual(handle["task_id"], card["id"])
        self.assertEqual(len(self.kb.created), 1)
        conn = self.kb.connect(board="demo")
        self.kb.archive_task(conn, card["id"])
        conn.close()
        self.adapter.schedule(node, run, "demo", **{**args, "owner": "new"})
        self.assertEqual(len(self.kb.created), 1)

    def test_restart_resumes_same_card_without_cancelling_it(self):
        runtime = self.runtime()
        run = runtime.engine.start(workflow())
        card = self.wait_card()
        identity = self.db.get_run(run["run_id"])["nodes"]["work"]["attempt_id"]
        runtime.close()
        self.assertEqual(self.kb.cards()[0]["status"], "ready")
        restarted = self.runtime()
        self.wait(lambda: self.db.get_run(run["run_id"])["nodes"]["work"]["attempt_id"] == identity)
        self.kb.finish(card["id"])
        self.assertEqual(self.settled(run["run_id"])["status"], "completed")
        self.assertEqual(len(self.kb.created), 1)
        self.assertEqual(self.db.get_run(run["run_id"])["nodes"]["work"]["attempt_id"], identity)
        self.assertFalse(restarted.closed)

    def test_cancel_after_runtime_shutdown_reaps_only_owned_card(self):
        runtime = self.runtime()
        run = runtime.engine.start(workflow())
        owned = self.wait_card()
        with self.kb.host().scope():
            conn = self.kb.connect(board="demo")
            other = self.kb.create_task(
                conn,
                title="unrelated",
                body="",
                assignee="qa",
                created_by="user",
                idempotency_key="unrelated",
            )
            conn.close()
        runtime.close()
        runtime.engine.cancel(run["run_id"])
        cards = {c["id"]: c for c in self.kb.cards()}
        self.assertEqual(cards[owned["id"]]["status"], "archived")
        self.assertEqual(cards[other]["status"], "ready")

    def test_timeout_closes_card_even_if_workflow_parks_at_review(self):
        runtime = self.runtime()
        wf = workflow()
        wf["nodes"][0]["timeout_seconds"] = 0.15
        wf["nodes"][1] = {"id": "done", "type": "human_review"}
        run = runtime.engine.start(wf)
        card = self.wait_card()
        self.assertEqual(self.settled(run["run_id"])["status"], "waiting")
        self.wait(lambda: self.kb.cards()[0]["status"] == "archived")
        self.assertIn(card["id"], self.kb.archived)

    def test_loop_reentry_creates_a_new_card_and_preserves_attempt_handles(self):
        runtime = self.runtime()
        wf = workflow()
        wf["nodes"].insert(1, {"id": "review", "type": "human_review"})
        wf["edges"] = [
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
        # A loop's entry cannot have a feedback edge; add a routing entry.
        wf["nodes"].insert(0, {"id": "start", "type": "prompt"})
        wf["edges"].insert(0, {"from": "start", "to": "work"})
        run = runtime.engine.start(wf)
        card = self.wait_card()
        self.kb.finish(card["id"])
        first = self.settled(run["run_id"])
        runtime.engine.review(
            run["run_id"],
            "review",
            "needs_changes",
            attempt_id=first["nodes"]["review"]["attempt_id"],
        )
        second = self.wait_card(1)
        self.assertNotEqual(card["id"], second["id"])
        self.kb.finish(second["id"])
        next_review = self.settled(run["run_id"])
        runtime.engine.review(
            run["run_id"],
            "review",
            "approved",
            attempt_id=next_review["nodes"]["review"]["attempt_id"],
        )
        self.assertEqual(self.settled(run["run_id"])["status"], "completed")
        self.assertEqual(
            len(
                [
                    a
                    for a in self.db.attempts(run["run_id"])
                    if a["node_id"] == "work" and a["handle"]
                ]
            ),
            2,
        )

    def test_profile_identity_and_board_path_changes_cannot_redirect_an_attempt(self):
        wf, run, node = self.seeded()
        token = run["nodes"]["work"]["attempt_id"]
        handle = self.adapter.schedule(
            node, run, "demo", owner="old", revision=0, attempt_id=token, timeout=30
        )
        other = KanbanExecutor(self.db, self.kb.host("other"))
        with self.assertRaisesRegex(Exception, "different profile"):
            other.schedule(node, run, "demo", owner="old", revision=0, attempt_id=token, timeout=30)
        self.assertEqual(len(self.kb.created), 1)
        with patch.object(self.kb, "kanban_db_path", return_value=self.root / "wrong.db"):
            with self.assertRaisesRegex(Exception, "pinned"):
                self.adapter.schedule(
                    node, run, "demo", owner="old", revision=0, attempt_id=token, timeout=30
                )
        self.assertEqual(self.kb.cards()[0]["id"], handle["task_id"])

    def test_real_process_crash_after_card_commit_recovers_without_duplicate(self):
        wf, run, node = self.seeded()
        self.kb.create_board("demo")
        plugin = Path(__file__).resolve().parents[1] / "hermes-workflows-py"
        child = """
import json, os, sys
from pathlib import Path
from test_kanban import BoardDouble
from hermes_workflows.db import Database
from hermes_workflows.kanban import KanbanExecutor
db=Database(Path(sys.argv[1]))
kb=BoardDouble(Path(sys.argv[2])); kb.create_board('demo')
adapter=KanbanExecutor(db,kb.host())
adapter.effects.attach_handle=lambda *a,**k: os._exit(71)
run=db.get_run(sys.argv[3])
node={'id':'work','type':'agent_task','prompt':'Do the work','profile':'qa'}
adapter.schedule(node,run,'demo',owner='old',revision=0,attempt_id=run['nodes']['work']['attempt_id'],timeout=30)
"""
        result = subprocess.run(
            [sys.executable, "-c", child, str(self.db.path), str(self.kb.root), run["run_id"]],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join([str(plugin), str(Path(__file__).parent)]),
            },
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 71, result.stderr.decode(errors="replace"))
        self.assertEqual(len(self.kb.cards()), 1)
        identity = self.kb.cards()[0]["id"]
        self.db.release(run["run_id"], "old")
        self.db.claim(run["run_id"], "new")
        self.adapter.effects.recover(run["run_id"], owner="new", revision=0)
        handle = self.adapter.schedule(
            node,
            run,
            "demo",
            owner="new",
            revision=0,
            attempt_id=run["nodes"]["work"]["attempt_id"],
            timeout=30,
        )
        self.assertEqual(handle["task_id"], identity)
        self.assertEqual(len(self.kb.cards()), 1)

    def test_native_metadata_is_committed_before_dispatch_can_see_card(self):
        wf, run, node = self.seeded()
        create = self.kb.create_task

        def observed(conn, **kwargs):
            identity = create(conn, **kwargs)
            with closing(self.kb.connect(board="demo")) as reader:
                self.assertEqual(reader.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
            return identity

        with patch.object(self.kb, "create_task", side_effect=observed):
            self.adapter.schedule(
                node,
                run,
                "demo",
                owner="old",
                revision=0,
                attempt_id=run["nodes"]["work"]["attempt_id"],
                timeout=30,
            )
        self.assertEqual(self.kb.cards()[0]["created_by"], CREATOR)
        self.assertEqual(self.kb.cards()[0]["workflow_template_id"], wf["id"])

    def test_manual_retry_gets_a_new_card_identity(self):
        runtime = self.runtime()
        wf = workflow()
        run = runtime.engine.start(wf)
        first = self.wait_card()
        runtime.engine.cancel(run["run_id"])
        runtime.engine.retry(run["run_id"], wf)
        second = self.wait_card(1)
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        self.kb.finish(second["id"])
        self.assertEqual(self.settled(run["run_id"])["status"], "completed")
        self.assertEqual(self.kb.cards()[0]["status"], "archived")

    def test_cancel_waits_for_unacknowledged_creation_before_cleanup(self):
        wf, run, node = self.seeded()
        engine = Engine(self.db)
        engine.kanban = self.adapter
        inserted, release = threading.Event(), threading.Event()
        original = self.kb.create_task
        failures = []

        def create(conn, **kwargs):
            identity = original(conn, **kwargs)
            inserted.set()
            if not release.wait(5):
                raise AssertionError("test did not release creator")
            return identity

        def schedule():
            try:
                self.adapter.schedule(
                    node,
                    run,
                    "demo",
                    owner="old",
                    revision=0,
                    attempt_id=run["nodes"]["work"]["attempt_id"],
                    timeout=30,
                )
            except Exception as exc:
                failures.append(exc)

        with patch.object(self.kb, "create_task", side_effect=create):
            creator = threading.Thread(target=schedule)
            creator.start()
            try:
                self.assertTrue(inserted.wait(5))
                cancelling = threading.Thread(target=lambda: engine.cancel(run["run_id"]))
                cancelling.start()
                self.wait(lambda: self.db.get_run(run["run_id"])["status"] == "cancelled")
            finally:
                release.set()
                creator.join(5)
            cancelling.join(5)
        self.assertFalse(creator.is_alive())
        self.assertFalse(cancelling.is_alive())
        self.assertTrue(failures)
        self.assertEqual(len(self.kb.cards()), 1)
        self.assertEqual(self.kb.cards()[0]["status"], "archived")

    def test_archived_board_and_unrepresentable_overrides_fail_explicitly(self):
        wf, run, node = self.seeded()
        self.kb.create_board("demo")
        self.kb.boards["demo"]["archived"] = True
        args = dict(
            owner="old", revision=0, attempt_id=run["nodes"]["work"]["attempt_id"], timeout=30
        )
        with self.assertRaisesRegex(Exception, "archived"):
            self.adapter.schedule(node, run, "demo", **args)
        self.assertTrue(self.kb.boards["demo"]["archived"])
        self.kb.boards["demo"]["archived"] = False
        with self.assertRaisesRegex(Exception, "board:false"):
            self.adapter.schedule({**node, "disabled_toolsets": ["terminal"]}, run, "demo", **args)
        self.assertEqual(self.kb.cards(), [])

    def test_completion_handles_metadata_failures_and_archival(self):
        task = {"status": "done", "result": "old"}
        self.assertEqual(
            completion(
                task, {"outcome": "completed", "summary": "HTTP 429", "metadata": "{}"}
            ).outcome,
            "failure",
        )
        self.assertEqual(
            completion(
                task,
                {
                    "outcome": "completed",
                    "summary": "HTTP 429",
                    "metadata": '{"node_outcome":"success"}',
                },
            ).outcome,
            "success",
        )
        self.assertEqual(
            completion(task, {"outcome": "failed", "summary": "bad", "metadata": "{oops"}).outcome,
            "failure",
        )
        self.assertEqual(
            completion({"status": "archived", "result": None}, None).outcome, "failure"
        )

    def test_failed_native_worker_cannot_be_overridden_by_summary_text(self):
        task = {"status": "done", "result": "old"}
        latest = {
            "outcome": "failed",
            "summary": '{"node_outcome":"success"}',
            "metadata": "{}",
        }
        self.assertEqual(completion(task, latest).outcome, "failure")
        # Pinned upstream gives explicit native metadata precedence over
        # outcome; stdout tokens are considered only for completed workers.
        latest["metadata"] = '{"node_outcome":"success"}'
        self.assertEqual(completion(task, latest).outcome, "success")
        latest.update(outcome="completed", metadata="{}")
        self.assertEqual(completion(task, latest).outcome, "success")
