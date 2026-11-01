from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.adoption import (
    AdoptionExecutor,
    extract_task_ids,
    ordered_scope,
    resolve_task_ref,
)
from hermes_workflows.db import ConflictError, Database
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Executor, Result
from hermes_workflows.kanban import KanbanExecutor
from hermes_workflows.runtime import Runtime
from hermes_workflows.specs import SpecStore, compile_preview, resolve, validate
from test_kanban import BoardDouble


class AdoptionBoard(BoardDouble):
    @staticmethod
    def _canonical_assignee(assignee):
        return assignee.strip().lower() if assignee else None

    @staticmethod
    def _schema(conn):
        BoardDouble._schema(conn)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "consecutive_failures" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN consecutive_failures INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE tasks ADD COLUMN last_failure_error TEXT")
            conn.execute("ALTER TABLE task_runs ADD COLUMN profile TEXT DEFAULT 'qa'")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_links(parent_id TEXT,child_id TEXT);
            CREATE TABLE IF NOT EXISTS task_events(id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,payload TEXT);
        """)

    def _append_event(self, conn, task_id, kind, payload):
        conn.execute(
            "INSERT INTO task_events(task_id,kind,payload) VALUES(?,?,?)",
            (task_id, kind, json.dumps(payload)),
        )

    def card(self, title="User card", status="triage"):
        with self.host().scope(), closing(self.connect(board="demo")) as conn:
            identity = self.create_task(
                conn,
                title=title,
                body="original user instructions",
                assignee="original",
                created_by="user",
                idempotency_key="user:" + str(time.time_ns()),
            )
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, identity))
            return identity

    def update(self, identity, **values):
        with closing(self.connect(board="demo")) as conn:
            conn.execute(
                "UPDATE tasks SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
                (*values.values(), identity),
            )

    def link(self, parent, child):
        with closing(self.connect(board="demo")) as conn:
            conn.execute("INSERT INTO task_links VALUES(?,?)", (parent, child))


def workflow(ref, **options):
    return {
        "id": "adopt-test",
        "name": "Adopt",
        "version": 1,
        "scope": {"type": "project", "projects": ["demo"]},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "qa"},
        "nodes": [
            {
                "id": "drive",
                "type": "agent_task",
                "prompt": "Existing card instructions apply",
                "adopt": True,
                "task_ref": ref,
                **options,
            },
            {"id": "done", "type": "finish"},
        ],
        "edges": [
            {
                "from": "drive",
                "to": "done",
                "condition": {"type": "node_status", "node": "drive", "equals": "success"},
            }
        ],
    }


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow adoption żółć ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "runs.db")
        self.board = AdoptionBoard(self.root / "native")
        self.board.create_board("demo")
        self.adapter = KanbanExecutor(self.db, self.board.host(), poll_seconds=0.01)
        self.adapter.adoption = AdoptionExecutor(self.adapter)
        self.runtimes = []

    def runtime(self, output=""):
        async def collect(node, run):
            return Result(output=output)

        runtime = Runtime(self.db, SpecStore([self.root / "specs"]), Executor(agent_runner=collect))
        runtime.engine.kanban = self.adapter
        self.runtimes.append(runtime)
        self.addCleanup(runtime.close)
        return runtime.start()

    def wait(self, predicate):
        started = time.monotonic()
        deadline = started + 6
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.01)
        runs = [self.db.get_run(row["run_id"]) for row in self.db.list_runs("all")]
        diagnostics = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "runs": runs,
            "attempts": {run["run_id"]: self.db.attempts(run["run_id"]) for run in runs},
            "cards": self.board.cards(),
            "runtimes": [
                {
                    "owner": runtime.owner,
                    "thread_alive": bool(runtime.thread and runtime.thread.is_alive()),
                    "startup_error": repr(runtime._startup_error),
                }
                for runtime in self.runtimes
            ],
        }
        self.fail("adoption did not settle: " + json.dumps(diagnostics, default=str))

    def state(self, run):
        return self.db.get_run(run["run_id"])

    def terminal(self, run):
        return self.wait(
            lambda: (
                s
                if (s := self.state(run))["status"] in ("completed", "failed", "cancelled")
                else None
            )
        )

    def card(self, identity):
        return next(c for c in self.board.cards() if c["id"] == identity)

    def collected(self, ids, **options):
        wf = workflow("{{nodes.collect.output.task_ids}}", **options)
        wf["nodes"].insert(
            0, {"id": "collect", "type": "agent_task", "prompt": "Find cards", "board": False}
        )
        wf["edges"].insert(0, {"from": "collect", "to": "drive"})
        output = (
            "Ignore stray t_999999.\n```task_ids\n" + "\n".join(ids) + "\n```"
            if ids
            else "No cards selected"
        )
        return wf, self.runtime(output)

    def test_literal_adoption_preserves_identity_body_creator_and_card_count(self):
        identity = self.board.card()
        runtime = self.runtime()
        run = runtime.engine.start(workflow(identity))
        self.wait(lambda: self.card(identity)["status"] == "ready")
        card = self.card(identity)
        self.assertEqual(
            (card["assignee"], card["created_by"], card["body"]),
            ("qa", "user", "original user instructions"),
        )
        self.board.finish(identity, output="final native result")
        final = self.terminal(run)
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(final["nodes"]["drive"]["output"], "final native result")
        self.assertEqual(len(self.board.cards()), 1)
        handle = next(
            a["handle"] for a in self.db.attempts(run["run_id"]) if a["node_id"] == "drive"
        )
        self.assertEqual(handle["task_ids"], [identity])

    def test_structured_output_selects_only_chosen_cards_in_parallel(self):
        ids = [self.board.card(), self.board.card()]
        wf, runtime = self.collected(ids)
        run = runtime.engine.start(wf)
        self.wait(lambda: all(self.card(i)["status"] == "ready" for i in ids))
        self.assertEqual(self.state(run)["nodes"]["collect"]["task_ids"], ids)
        self.board.finish(ids[0])
        self.assertEqual(self.state(run)["status"], "running")
        self.board.finish(ids[1])
        self.assertEqual(self.terminal(run)["status"], "completed")
        self.assertEqual(len(self.board.cards()), 2)

    def test_missing_unknown_and_empty_scopes_abort_before_downstream_success(self):
        for choice in ("missing", "empty", "partly_missing", "unbound"):
            with self.subTest(choice=choice):
                identity = self.board.card()
                wf, runtime = self.collected([] if choice == "empty" else [identity, "t_absent"])
                if choice == "missing":
                    wf = workflow("t_absent")
                if choice == "unbound":
                    wf = workflow(identity)
                    wf["scope"] = {"type": "global"}
                final = self.terminal(runtime.engine.start(wf))
                self.assertEqual(final["status"], "failed", final)
                self.assertTrue(final["nodes"]["drive"]["abort_run"])
                self.assertNotEqual(final["nodes"]["done"]["status"], "completed")
                self.assertEqual(self.card(identity)["status"], "triage")
                runtime.close()

    def test_dependency_order_and_explicit_sequence_wait_for_each_card(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                first, second = self.board.card(), self.board.card()
                if linked:
                    self.board.link(first, second)
                wf, runtime = self.collected(
                    [second, first] if linked else [first, second], sequential=not linked
                )
                run = runtime.engine.start(wf)
                self.wait(lambda first=first: self.card(first)["status"] == "ready")
                self.assertEqual(self.card(second)["status"], "triage")
                self.board.finish(first, output="first")
                self.wait(lambda second=second: self.card(second)["status"] == "ready")
                self.board.finish(second, output="second")
                final = self.terminal(run)
                self.assertEqual(final["status"], "completed", final)
                self.assertEqual(final["nodes"]["drive"]["output"], "first\n\nsecond")
                runtime.close()

    def test_sequence_continues_after_failure_and_aggregates_failure(self):
        first, second = self.board.card(), self.board.card()
        wf, runtime = self.collected([first, second], sequential=True)
        run = runtime.engine.start(wf)
        self.wait(lambda: self.card(first)["status"] == "ready")
        self.board.finish(first, outcome="failed", output="first failed")
        self.wait(lambda: self.card(second)["status"] == "ready")
        self.board.finish(second, output="second passed")
        final = self.terminal(run)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["nodes"]["drive"]["output"], "first failed\n\nsecond passed")

    def test_umbrella_and_cyclic_or_external_dependencies_fail_without_unlinking(self):
        parent, child = self.board.card("(META) umbrella"), self.board.card()
        self.board.link(parent, child)
        for ids in ([parent], [parent, child]):
            wf, runtime = self.collected(ids)
            final = self.terminal(runtime.engine.start(wf))
            self.assertEqual(final["status"], "failed")
            self.assertTrue(final["nodes"]["drive"]["abort_run"])
            runtime.close()
        self.board.link(child, parent)
        wf, runtime = self.collected([child, parent])
        final = self.terminal(runtime.engine.start(wf))
        self.assertEqual(final["status"], "failed")
        self.assertEqual(self.card(parent)["status"], "triage")
        self.assertEqual(self.card(child)["status"], "triage")
        with closing(self.board.connect(board="demo")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_links").fetchone()[0], 2)

    def test_review_uses_a_second_native_result_and_only_one_handoff(self):
        identity = self.board.card()
        runtime = self.runtime()
        run = runtime.engine.start(workflow(identity, review_profile="reviewer"))
        self.wait(lambda: self.card(identity)["status"] == "ready")
        self.board.finish(identity, output="implementer")
        self.wait(lambda: self.card(identity)["status"] == "review")
        self.assertEqual(self.card(identity)["assignee"], "reviewer")
        runtime.close()
        # A fast reviewer finishes while the supervisor is offline.
        self.board.finish(identity, output="review final")
        self.runtime()
        final = self.terminal(run)
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(final["nodes"]["drive"]["output"], "review final")
        with closing(self.board.connect(board="demo")) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM task_events WHERE kind='review_requested'"
                ).fetchone()[0],
                1,
            )

    def test_running_and_review_cards_are_not_reassigned(self):
        for status in ("running", "review"):
            identity = self.board.card(status=status)
            runtime = self.runtime()
            run = runtime.engine.start(workflow(identity))
            self.wait(lambda run=run: self.state(run)["nodes"]["drive"].get("adoption"))
            self.assertEqual(self.card(identity)["assignee"], "original")
            self.board.finish(identity)
            self.assertEqual(self.terminal(run)["status"], "completed")
            runtime.close()

    def test_cancel_retry_and_timeout_never_archive_or_recreate_user_cards(self):
        identity = self.board.card()
        runtime = self.runtime()
        wf = workflow(identity, timeout_seconds=30)
        run = runtime.engine.start(wf)
        self.wait(lambda: self.card(identity)["status"] == "ready")
        runtime.engine.cancel(run["run_id"])
        self.assertEqual(self.card(identity)["status"], "ready")
        wf["nodes"][0]["timeout_seconds"] = 0.15
        runtime.engine.retry(run["run_id"], wf)
        final = self.terminal(run)
        self.assertEqual(final["status"], "failed")
        self.assertIn("timed out", final["nodes"]["drive"]["error"])
        self.assertEqual(len(self.board.cards()), 1)
        self.assertFalse(self.board.archived)

    def test_stuck_dispatch_is_bounded_but_running_failures_are_not_stuck(self):
        identity = self.board.card()
        runtime = self.runtime()
        run = runtime.engine.start(workflow(identity))
        self.wait(lambda: self.card(identity)["status"] == "ready")
        self.board.update(identity, status="running", consecutive_failures=9)
        time.sleep(0.05)
        self.assertEqual(self.state(run)["status"], "running")
        self.board.update(identity, status="review")
        final = self.terminal(run)
        self.assertIn("adopt stuck", final["nodes"]["drive"]["error"])
        self.assertEqual(self.card(identity)["status"], "review")

    def test_blocked_clock_survives_restart_and_resets_after_recovery(self):
        identity = self.board.card()
        runtime = self.runtime()
        self.adapter.adoption.blocked_timeout_seconds = 0.25
        run = runtime.engine.start(workflow(identity))
        self.wait(lambda: self.card(identity)["status"] == "ready")
        self.board.update(identity, status="blocked")
        self.wait(lambda: self.state(run)["nodes"]["drive"]["adoption"]["blocked_since"])
        self.board.update(identity, status="running")
        self.wait(lambda: self.state(run)["nodes"]["drive"]["adoption"]["blocked_since"] is None)
        self.board.update(identity, status="blocked")
        self.wait(lambda: self.state(run)["nodes"]["drive"]["adoption"]["blocked_since"])
        runtime.close()
        time.sleep(0.3)
        self.runtime()
        final = self.terminal(run)
        self.assertIn("adopt blocked", final["nodes"]["drive"]["error"])
        self.assertEqual(self.card(identity)["status"], "blocked")

    def test_extraction_reference_precedence_and_pinned_ordering(self):
        text = "ignore t_wrong ```task_ids\nt_a\nt_b\nt_a\n``` <task_ids>t_c</task_ids>"
        self.assertEqual(extract_task_ids(text), ["t_a", "t_b", "t_c"])
        run = {
            "nodes": {"collect": {"status": "completed", "task_ids": ["t_chosen"], "output": text}}
        }
        self.assertEqual(resolve_task_ref(run, "{{nodes.collect.output.task_ids}}"), ["t_chosen"])
        self.assertEqual(resolve("{{nodes.collect.output.task_ids}}", run), '["t_chosen"]')
        run["nodes"]["collect"].pop("task_ids")
        self.assertEqual(
            resolve_task_ref(run, "{{nodes.collect.output.task_ids}}"), ["t_a", "t_b", "t_c"]
        )
        run["nodes"]["collect"]["output"] = "legacy t_a t_b t_a"
        self.assertEqual(resolve_task_ref(run, "{{nodes.collect.output.task_ids}}"), ["t_a", "t_b"])
        self.assertEqual(ordered_scope(["c", "a", "b"], [("a", "c"), ("b", "c")]), ["a", "b", "c"])
        self.assertEqual(ordered_scope(["c", "a", "b"], [("a", "b"), ("b", "a")]), ["c", "a", "b"])

    def test_validation_and_compile_preserve_adoption_options(self):
        wf = workflow("t_a", review_profile="reviewer", sequential=True)
        self.assertTrue(validate(wf)["valid"])
        compiled = compile_preview(wf)["kanban_tasks"][0]
        self.assertTrue(compiled["adopt"])
        self.assertEqual(compiled["task_ref"], "t_a")
        self.assertEqual(compiled["review_profile"], "reviewer")
        for override in (
            {"task_ref": ""},
            {"task_ref": "{{nodes.absent.output.task_ids}}"},
            {"adopt": False},
            {"sequential": "yes"},
            {"board": False},
        ):
            bad = workflow("t_a")
            bad["nodes"][0].update(override)
            self.assertFalse(validate(bad)["valid"], override)

    def seeded(self, identity):
        engine = Engine(self.db)
        engine.kanban = self.adapter
        wf = workflow(identity, review_profile="reviewer")
        run = engine.start(wf)
        self.assertTrue(self.db.claim(run["run_id"], "old"))

        def begin(state):
            state["status"] = "running"
            state["nodes"]["drive"].update(
                status="running",
                executor="kanban",
                started_at=time.time(),
                execution_timeout=20,
                executor_home=str(self.adapter.host.home),
            )

        run = self.db.mutate_run(run["run_id"], begin, owner="old")
        node = {**wf["nodes"][0], "profile": "qa"}
        plan = self.adapter.adoption.plan(node, run, "demo")
        args = (run["run_id"], "old", 0, "drive", run["nodes"]["drive"]["attempt_id"])
        self.adapter.adoption.save(plan, args)
        return engine, run, plan, args

    def test_real_crash_after_review_commit_cannot_repeat_fast_review(self):
        identity = self.board.card()
        _, run, _, _ = self.seeded(identity)
        plugin = Path(__file__).resolve().parents[1] / "hermes-workflows-py"
        child = """
import os,sys
from pathlib import Path
from test_adoption import AdoptionBoard
from hermes_workflows.db import Database
from hermes_workflows.kanban import KanbanExecutor
from hermes_workflows.adoption import AdoptionExecutor
db=Database(Path(sys.argv[1])); board=AdoptionBoard(Path(sys.argv[2])); board.create_board('demo')
kb=KanbanExecutor(db,board.host()); adoption=AdoptionExecutor(kb)
run=db.get_run(sys.argv[3]); plan=run['nodes']['drive']['adoption']; identity=plan['ordered'][0]
args=(run['run_id'],'old',0,'drive',run['nodes']['drive']['attempt_id'])
adoption.action(plan,identity,'adopt',args)
board.finish(identity,output='implementer')
adoption.action(plan,identity,'review',args)
os._exit(72)
"""
        result = subprocess.run(
            [sys.executable, "-c", child, str(self.db.path), str(self.board.root), run["run_id"]],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join((str(plugin), str(Path(__file__).parent))),
            },
            capture_output=True,
            timeout=25,
        )
        self.assertEqual(result.returncode, 72, result.stderr.decode(errors="replace"))
        self.assertEqual(self.card(identity)["status"], "review")
        self.board.finish(identity, output="fast reviewer completed before recovery")
        self.db.release(run["run_id"], "old")
        self.runtime()
        final = self.terminal(run)
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(
            final["nodes"]["drive"]["output"], "fast reviewer completed before recovery"
        )
        with closing(self.board.connect(board="demo")) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM task_events WHERE kind='review_requested'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM task_runs").fetchone()[0], 2)
        self.assertEqual(len(self.board.cards()), 1)

    def test_cancellation_fences_pending_adoption_and_review_actions(self):
        identity = self.board.card()
        engine, run, plan, args = self.seeded(identity)
        engine.cancel(run["run_id"])
        for stage in ("adopt", "review"):
            with self.assertRaises(ConflictError):
                self.adapter.adoption.action(plan, identity, stage, args)
        self.assertEqual(self.card(identity)["status"], "triage")
        self.assertFalse(self.board.archived)

    def test_missing_next_sequential_card_aborts_without_recreating_it(self):
        first, second = self.board.card(), self.board.card()
        wf, runtime = self.collected([first, second], sequential=True)
        run = runtime.engine.start(wf)
        self.wait(lambda: self.card(first)["status"] == "ready")
        with closing(self.board.connect(board="demo")) as conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (second,))
        self.board.finish(first)
        final = self.terminal(run)
        self.assertEqual(final["status"], "failed")
        self.assertTrue(final["nodes"]["drive"]["abort_run"])
        self.assertEqual(len(self.board.cards()), 1)

    def test_actual_cycle_is_rejected_by_native_gates_without_promoting_cards(self):
        a, b = self.board.card(), self.board.card()
        self.board.link(a, b)
        self.board.link(b, a)
        wf, runtime = self.collected([a, b])
        final = self.terminal(runtime.engine.start(wf))
        self.assertEqual(final["status"], "failed")
        self.assertIn("unfinished parents", final["nodes"]["drive"]["error"])
        self.assertTrue(all(self.card(i)["status"] == "triage" for i in (a, b)))

    def test_persisted_adoption_plan_cannot_cross_profile_homes(self):
        identity = self.board.card()
        _, _, plan, args = self.seeded(identity)
        other = AdoptionExecutor(KanbanExecutor(self.db, self.board.host("other")))
        with self.assertRaisesRegex(ValueError, "different profile"):
            other.action(plan, identity, "adopt", args)
        self.assertEqual(self.card(identity)["status"], "triage")

    def test_frozen_pinned_upstream_extraction_and_ordering_decisions(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/adoption-reference.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["reference"]["commit"], "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f")
        for case in fixture["extraction"]:
            self.assertEqual(extract_task_ids(case["text"]), case["ids"])
        for case in fixture["ordering"]:
            self.assertEqual(ordered_scope(case["ids"], case["links"]), case["ordered"])

    def test_input_error_before_admission_cannot_fall_through_to_success(self):
        identity = self.board.card()
        wf, runtime = self.collected([identity])
        wf["nodes"][1].update(task_ref=identity, prompt="{{nodes.collect.output.missing}}")
        # Prose is not a JSON object; resolving its missing field fails before
        # card admission, and must never route an unconditioned finish.
        wf["edges"][-1].pop("condition")
        final = self.terminal(runtime.engine.start(wf))
        self.assertEqual(final["status"], "failed")
        self.assertTrue(final["nodes"]["drive"]["abort_run"])
        self.assertEqual(self.card(identity)["status"], "triage")
        self.assertNotEqual(final["nodes"]["done"]["status"], "completed")
