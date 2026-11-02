from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows import _tool
from hermes_workflows.api import API
from hermes_workflows.db import Database
from hermes_workflows.engine import Engine, selected_edges
from hermes_workflows.executor import Executor, Result
from hermes_workflows.graph import analyse
from hermes_workflows.runtime import Runtime
from hermes_workflows.settings import save
from hermes_workflows.specs import SpecStore, compile_preview, resolve, validate


def workflow():
    return {
        "id": "loop-check",
        "name": "Loop check",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "default"},
        "nodes": [
            {"id": "start", "type": "prompt", "prompt": "Check carefully"},
            {"id": "check", "type": "agent_task", "prompt": "Validate"},
            {"id": "fix", "type": "agent_task", "prompt": "Repair {{nodes.check.output}}"},
            {"id": "review", "type": "human_review"},
            {"id": "done", "type": "finish"},
            {"id": "bad", "type": "finish", "outcome": "failure"},
        ],
        "edges": [
            {"from": "start", "to": "check"},
            {
                "from": "check",
                "to": "review",
                "condition": {"type": "node_status", "node": "check", "equals": "success"},
            },
            {"from": "check", "to": "fix", "fallback": True},
            {"from": "fix", "to": "check"},
            {
                "from": "review",
                "to": "fix",
                "condition": {"type": "review_status", "equals": "needs_changes"},
            },
            {
                "from": "review",
                "to": "done",
                "condition": {"type": "review_status", "equals": "approved"},
            },
            {
                "from": "review",
                "to": "bad",
                "condition": {"type": "review_status", "equals": "rejected"},
            },
        ],
    }


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow loop żółć ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "runs.db")
        self.store = SpecStore([self.root / "global"])

    def runtime(self, runner=None, executor=None):
        runtime = Runtime(self.db, self.store, executor or Executor(agent_runner=runner))
        self.addCleanup(runtime.close)
        return runtime.start()

    def wait(
        self,
        run_id,
        predicate=lambda r: r["status"] in ("waiting", "completed", "failed", "cancelled"),
        *,
        timeout=6,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.db.get_run(run_id)
            if predicate(run):
                return run
            time.sleep(0.01)
        attempts = [
            (a["node_id"], a["iteration"], a["attempt"], a["status"])
            for a in self.db.attempts(run_id)
        ]
        with self.db.connect() as con:
            lease = dict(con.execute(
                "SELECT lease_owner,lease_until,revision FROM py_runs WHERE run_id=?",
                (run_id,),
            ).fetchone())
        self.fail(f"loop did not settle within {timeout}s: {run}; attempts={attempts}; lease={lease}")

    def test_pinned_pure_decision_reference(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/loop-reference.json").read_text())
        self.assertEqual(fixture["reference"]["commit"], "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f")
        self.assertEqual(len(fixture["reference"]["source_sha256"]), 64)
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                wf = fixture["workflows"][case["workflow"]]
                run = copy.deepcopy(case["run"])
                if case.get("source"):
                    self.assertEqual(
                        [e["to"] for e in selected_edges(wf, run, case["source"])], case["selected"]
                    )
                ready = Engine(self.db)._plan(run, wf)
                kinds = {node["id"]: node["type"] for node in wf["nodes"]}
                expected_status = case.get("python_expectation", {}).get(
                    "run_status", case["expected"]["run_status"]
                )
                self.assertEqual(run["status"], expected_status)
                if case["name"] == "review-reset":
                    self.assertNotIn("review_decision", run["nodes"]["review"])
                self.assertEqual(
                    [name for name in ready if kinds[name] != "wait"], case["expected"]["schedule"]
                )
                for name, expected in case["expected"]["node_updates"].items():
                    actual = run["nodes"][name]["status"]
                    if name in ready:
                        actual = "running" if kinds[name] == "wait" else "scheduled"
                    self.assertEqual(actual, expected, name)
                if "finish_outcome" in case["expected"]:
                    self.assertEqual(run["finish_outcome"], case["expected"]["finish_outcome"])

    def test_cycle_validation_compile_and_invalid_isolated_cycles(self):
        wf = workflow()
        result = validate(wf)
        self.assertTrue(result["valid"], result)
        self.assertIn("cycle", [w["code"] for w in result["warnings"]])
        self.assertEqual(compile_preview(wf)["first_node"], "start")
        self.assertTrue(analyse(wf)[2])
        isolated = copy.deepcopy(wf)
        isolated["nodes"] += [{"id": "x", "type": "condition"}, {"id": "y", "type": "condition"}]
        isolated["edges"] += [{"from": "x", "to": "y"}, {"from": "y", "to": "x"}]
        self.assertIn("unreachable_node", [e["code"] for e in validate(isolated)["errors"]])
        no_entry = copy.deepcopy(wf)
        no_entry["edges"].append({"from": "fix", "to": "start"})
        self.assertIn("entry_count", [e["code"] for e in validate(no_entry)["errors"]])
        self_ref = copy.deepcopy(wf)
        self_ref["nodes"][1]["prompt"] = "{{nodes.check.output}}"
        self.assertIn("invalid_input_ref", [e["code"] for e in validate(self_ref)["errors"]])

    def test_two_failed_checks_reenter_then_reach_review_and_finish(self):
        calls, prompts = [], []

        async def runner(node, run):
            calls.append((node["id"], run["nodes"][node["id"]]["iteration"]))
            if node["id"] == "check":
                number = sum(name == "check" for name, _ in calls)
                return Result("failure" if number < 3 else "success", f"check-{number}")
            prompts.append(node["prompt"])
            return Result(output="fixed")

        runtime = self.runtime(runner)
        run_id = runtime.engine.start(workflow())["run_id"]
        run = self.wait(run_id)
        self.assertEqual(run["status"], "waiting", run)
        self.assertEqual(calls, [("check", 0), ("fix", 0), ("check", 1), ("fix", 1), ("check", 2)])
        self.assertIn("check-1", prompts[0])
        self.assertIn("check-2", prompts[1])
        runtime.engine.review(
            run_id, "review", "approved", attempt_id=run["nodes"]["review"]["attempt_id"]
        )
        done = self.wait(run_id, lambda r: r["status"] in ("completed", "failed"))
        self.assertEqual(done["status"], "completed", done)
        history = [a for a in self.db.attempts(run_id) if a["node_id"] == "check"]
        self.assertEqual([a["data"]["outcome"] for a in history], ["failure", "failure", "success"])
        self.assertEqual(len({a["attempt_id"] for a in history}), 3)

    def test_repeated_reviews_have_new_tokens_clear_notes_and_survive_restart(self):
        async def runner(node, run):
            return Result(output="current result")

        wf = workflow()
        wf["nodes"][2]["prompt"] = "Repair: {{nodes.review.review_note}}"
        self.store.save({"workflow": wf})
        runtime = self.runtime(runner)
        run_id = runtime.engine.start(wf)["run_id"]
        first = self.wait(run_id)
        old_token = first["nodes"]["review"]["attempt_id"]
        runtime.engine.review(run_id, "review", "needs_changes", "fix this", attempt_id=old_token)
        second = self.wait(
            run_id,
            lambda r: (
                r["status"] == "waiting" and r["nodes"]["review"].get("attempt_id") != old_token
            ),
        )
        new_token = second["nodes"]["review"]["attempt_id"]
        self.assertNotIn("review_note", second["nodes"]["review"])
        self.assertNotIn("review_decision", second["nodes"]["review"])
        self.assertEqual(second["nodes"]["review"]["iteration"], 1)
        runtime.close()
        restarted = self.runtime(runner)
        api = API(restarted)
        code, _ = api.handle(
            "POST",
            f"/runs/{run_id}/review",
            {"node_id": "review", "decision": "approved", "attempt_id": old_token},
        )
        self.assertEqual(code, 409)
        with patch("hermes_workflows.runtime.get_runtime", return_value=restarted):
            self.assertEqual(
                _tool(
                    "workflow_review",
                    {
                        "run_id": run_id,
                        "node_id": "review",
                        "decision": "approved",
                        "attempt_id": old_token,
                    },
                )[0],
                409,
            )
        self.assertEqual(self.db.get_run(run_id)["status"], "waiting")
        self.assertEqual(
            api.handle(
                "POST",
                f"/runs/{run_id}/review",
                {"node_id": "review", "decision": "approved", "attempt_id": new_token},
            )[0],
            200,
        )
        self.assertEqual(
            self.wait(run_id, lambda r: r["status"] in ("completed", "failed"))["status"],
            "completed",
        )
        reviews = [a for a in self.db.attempts(run_id) if a["node_id"] == "review"]
        self.assertEqual(
            [a["data"]["review_decision"] for a in reviews], ["needs_changes", "approved"]
        )

    def test_budget_failure_can_be_retried_with_a_new_budget(self):
        successful = False

        async def runner(node, run):
            return Result(
                "failure" if node["id"] == "check" and not successful else "success", "result"
            )

        save(self.db, {"max_loop_reentries": 2})
        runtime = self.runtime(runner)
        wf = workflow()
        run_id = runtime.engine.start(wf)["run_id"]
        # Multiple loop turns persist real SQLite transactions. On Windows CI
        # their I/O can exceed six seconds even while every turn progresses.
        failed = self.wait(run_id, timeout=30)
        self.assertEqual(failed["status"], "failed", failed)
        self.assertIn("loop re-entry limit", failed["error"])
        self.assertEqual(failed["loop_reentries"], 2)
        for node_id in ("check", "fix"):
            self.assertEqual(
                [a["iteration"] for a in self.db.attempts(run_id) if a["node_id"] == node_id],
                [0, 1],
            )
        successful = True
        save(self.db, {"max_loop_reentries": 10})
        runtime.engine.retry(run_id, wf, "check")
        waiting = self.wait(run_id, timeout=30)
        self.assertEqual(waiting["status"], "waiting", waiting)
        self.assertEqual(waiting["execution_revision"], 1)
        self.assertEqual(waiting["loop_reentries"], 0)
        runtime.engine.review(
            run_id, "review", "approved", attempt_id=waiting["nodes"]["review"]["attempt_id"]
        )
        self.assertEqual(
            self.wait(run_id, lambda r: r["status"] in ("completed", "failed"), timeout=30)["status"],
            "completed",
        )

    def test_router_only_self_loop_is_bounded_and_sequence_is_monotonic(self):
        wf = workflow()
        wf["nodes"] = [
            {"id": "start", "type": "prompt"},
            {"id": "route", "type": "condition"},
            {"id": "done", "type": "finish"},
        ]
        wf["edges"] = [
            {"from": "start", "to": "route"},
            {
                "from": "route",
                "to": "route",
                "condition": {"type": "node_status", "node": "route", "equals": "success"},
            },
            {"from": "route", "to": "done", "fallback": True},
        ]
        save(self.db, {"max_loop_reentries": 3})
        runtime = self.runtime()
        run_id = runtime.engine.start(wf)["run_id"]
        failed = self.wait(run_id)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("limit (3)", failed["error"])
        rows = [a for a in self.db.attempts(run_id) if a["node_id"] == "route"]
        self.assertEqual([a["iteration"] for a in rows], [0, 1, 2, 3])
        seq = [a["data"]["seq"] for a in rows]
        self.assertEqual(seq, sorted(set(seq)))

    def test_worker_self_loop_retries_on_new_domain_outcomes(self):
        seen = []

        async def runner(node, run):
            seen.append(run["nodes"]["work"]["iteration"])
            return Result("failure" if len(seen) < 3 else "success", "output")

        wf = workflow()
        wf["nodes"] = [
            {"id": "start", "type": "prompt"},
            {"id": "work", "type": "agent_task", "prompt": "work"},
            {"id": "done", "type": "finish"},
        ]
        wf["edges"] = [
            {"from": "start", "to": "work"},
            {
                "from": "work",
                "to": "work",
                "condition": {"type": "node_status", "node": "work", "equals": "failure"},
            },
            {"from": "work", "to": "done", "fallback": True},
        ]
        runtime = self.runtime(runner)
        run_id = runtime.engine.start(wf)["run_id"]
        result = self.wait(run_id)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(seen, [0, 1, 2])

    def test_selected_finish_wins_over_a_simultaneous_self_back_edge(self):
        calls = []

        async def runner(node, run):
            calls.append(node["id"])
            return Result(output="done")

        wf = workflow()
        wf["nodes"] = [
            {"id": "start", "type": "prompt"},
            {"id": "work", "type": "agent_task", "prompt": "work"},
            {"id": "done", "type": "finish"},
        ]
        wf["edges"] = [
            {"from": "start", "to": "work"},
            {"from": "work", "to": "work"},
            {"from": "work", "to": "done"},
        ]
        runtime = self.runtime(runner)
        run_id = runtime.engine.start(wf)["run_id"]
        self.assertEqual(self.wait(run_id)["status"], "completed")
        self.assertEqual(calls, ["work"])

    def test_wait_reentry_has_a_fresh_timeout_clock(self):
        starts = []

        class Stub(Executor):
            async def execute(self, node, run, settings):
                if node["type"] == "wait":
                    starts.append(run["nodes"]["merge"]["wait_started_at"])
                    return Result("failure" if len(starts) == 1 else "success", "PR")
                await asyncio.sleep(0.02)
                return Result(output="fixed")

        wf = workflow()
        wf["nodes"] = [
            {"id": "start", "type": "prompt"},
            {
                "id": "merge",
                "type": "wait",
                "wait_for": {"github_pr_merged": "https://github.com/a/b/pull/1"},
            },
            {"id": "fix", "type": "agent_task", "prompt": "fix"},
            {"id": "done", "type": "finish"},
        ]
        wf["edges"] = [
            {"from": "start", "to": "merge"},
            {
                "from": "merge",
                "to": "done",
                "condition": {"type": "node_status", "node": "merge", "equals": "success"},
            },
            {"from": "merge", "to": "fix", "fallback": True},
            {"from": "fix", "to": "merge"},
        ]
        runtime = self.runtime(executor=Stub())
        run_id = runtime.engine.start(wf)["run_id"]
        result = self.wait(run_id)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(starts), 2)
        self.assertGreater(starts[1], starts[0])

    def test_cancelling_a_loop_does_not_dispatch_another_iteration(self):
        entered = threading.Event()
        calls = []

        async def runner(node, run):
            calls.append(node["id"])
            if node["id"] == "check":
                return Result("failure", "failed check")
            entered.set()
            await asyncio.sleep(100)

        runtime = self.runtime(runner)
        run_id = runtime.engine.start(workflow())["run_id"]
        self.assertTrue(entered.wait(4))
        runtime.engine.cancel(run_id)
        runtime.close()
        self.assertEqual(self.db.get_run(run_id)["status"], "cancelled")
        self.assertEqual(calls, ["check", "fix"])

    def test_reaching_finish_cancels_an_unrelated_active_branch(self):
        entered, cancelled = threading.Event(), threading.Event()

        async def runner(node, run):
            if node["id"] == "short":
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                return Result(output="finished")
            entered.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                return Result(output="late output must not replace cancelled state")

        wf = workflow()
        wf["nodes"] = [
            {"id": "start", "type": "prompt"},
            *[{"id": name, "type": "agent_task", "prompt": name} for name in ("short", "long")],
            {"id": "done", "type": "finish"},
        ]
        wf["edges"] = [{"from": "start", "to": name} for name in ("short", "long")] + [
            {"from": "short", "to": "done"}
        ]
        runtime = self.runtime(runner)
        run_id = runtime.engine.start(wf)["run_id"]
        result = self.wait(run_id)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(cancelled.wait(2))
        self.assertEqual(self.db.get_run(run_id)["nodes"]["long"]["status"], "cancelled")
        self.assertNotIn("output", self.db.get_run(run_id)["nodes"]["long"])

    def test_interrupted_loop_work_requires_explicit_retry_after_restart(self):
        calls = []

        async def runner(node, run):
            calls.append(node["id"])
            return Result(output="recovered")

        wf = workflow()
        engine = Engine(self.db)
        run_id = engine.start(wf)["run_id"]
        self.db.mutate_run(
            run_id,
            lambda r: (
                r.update(status="running"),
                r["nodes"]["start"].update(status="completed", outcome="success", seq=1),
                r["nodes"]["check"].update(status="running", iteration=2),
            ),
        )
        runtime = self.runtime(runner)
        failed = self.wait(run_id)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("interrupted", failed["nodes"]["check"]["error"])
        self.assertEqual(calls, [])
        runtime.engine.retry(run_id, wf, "check")
        waiting = self.wait(run_id)
        self.assertEqual(waiting["status"], "waiting", waiting)
        runtime.engine.review(
            run_id, "review", "approved", attempt_id=waiting["nodes"]["review"]["attempt_id"]
        )
        self.assertEqual(
            self.wait(run_id, lambda r: r["status"] in ("completed", "failed"))["status"],
            "completed",
        )

    def test_missing_iteration_output_fails_and_injected_tokens_are_not_rescanned(self):
        run = {"params": {"x": "param"}, "nodes": {"a": {"status": "pending", "iteration": 1}}}
        with self.assertRaisesRegex(ValueError, "current iteration"):
            resolve("{{nodes.a.output}}", run)
        run["nodes"]["a"] = {"status": "completed", "output": "{{other}}"}
        self.assertEqual(
            resolve(
                "{{nodes.a.output}} {{value}}", run, {"value": "{{params.x}}", "other": "wrong"}
            ),
            "{{other}} {{params.x}}",
        )
        with self.assertRaisesRegex(ValueError, "no value for param"):
            resolve("{{params.missing}}", run)

    def test_input_mapping_substitutes_authored_placeholders_once(self):
        prompts = []

        async def runner(node, run):
            prompts.append(node["prompt"])
            return Result(output="{{other}}" if node["id"] == "a" else "done")

        wf = workflow()
        wf["nodes"] = [
            {"id": "a", "type": "agent_task", "prompt": "a"},
            {
                "id": "b",
                "type": "agent_task",
                "prompt": "Use {{value}}",
                "input_mapping": {"value": "{{nodes.a.output}}"},
            },
            {"id": "done", "type": "finish"},
        ]
        wf["edges"] = [{"from": "a", "to": "b"}, {"from": "b", "to": "done"}]
        runtime = self.runtime(runner)
        run_id = runtime.engine.start(wf)["run_id"]
        self.assertEqual(self.wait(run_id)["status"], "completed")
        self.assertEqual(prompts[-1], "Use {{other}}")
        wf["nodes"][1]["input_mapping"]["value"] = "not a reference"
        self.assertFalse(validate(wf)["valid"])

    def test_shipped_no_model_loop_executes_real_shell_iterations(self):
        example = (
            Path(__file__).resolve().parents[1]
            / "hermes-workflows-py/examples/review-loop.workflow.json"
        )
        wf = json.loads(example.read_text())
        save(self.db, {"scripts_enabled": True})
        runtime = self.runtime()
        run_id = runtime.engine.start(wf)["run_id"]
        first = self.wait(run_id)
        self.assertEqual(first["status"], "waiting", first)
        token = first["nodes"]["review"]["attempt_id"]
        runtime.engine.review(run_id, "review", "needs_changes", attempt_id=token)
        second = self.wait(
            run_id,
            lambda r: r["status"] == "waiting" and r["nodes"]["review"].get("attempt_id") != token,
        )
        self.assertIn("Ready for review", second["nodes"]["work"]["output"])
        self.assertEqual(second["nodes"]["work"]["iteration"], 1)
        runtime.engine.review(
            run_id, "review", "approved", attempt_id=second["nodes"]["review"]["attempt_id"]
        )
        self.assertEqual(
            self.wait(run_id, lambda r: r["status"] in ("completed", "failed"))["status"],
            "completed",
        )

    def test_cli_loop_reviews_fence_attempts_across_process_restarts(self):
        plugin = Path(__file__).resolve().parents[1] / "hermes-workflows-py"
        env = {
            **os.environ,
            "HERMES_HOME": str(self.root / "cli home"),
            "PYTHONPATH": str(plugin),
            "PYTHONIOENCODING": "utf-8",
        }

        def cli(*arguments, code=0):
            result = subprocess.run(
                [sys.executable, "-m", "hermes_workflows", *arguments],
                cwd=plugin,
                env=env,
                capture_output=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(result.returncode, code, result.stderr + result.stdout)
            return json.loads(result.stdout)

        wf = workflow()
        wf["nodes"] = [
            {"id": "start", "type": "prompt"},
            {"id": "review", "type": "human_review"},
            {"id": "again", "type": "condition"},
            {"id": "done", "type": "finish"},
        ]
        wf["edges"] = [
            {"from": "start", "to": "review"},
            {
                "from": "review",
                "to": "again",
                "condition": {"type": "review_status", "equals": "needs_changes"},
            },
            {"from": "again", "to": "review"},
            {
                "from": "review",
                "to": "done",
                "condition": {"type": "review_status", "equals": "approved"},
            },
        ]
        path = self.root / "cli-loop.json"
        path.write_text(json.dumps(wf), encoding="utf-8")
        cli("import", str(path))
        first = cli("run", wf["id"])
        old = first["nodes"]["review"]["attempt_id"]
        run_id = first["run_id"]
        second = cli("review", run_id, "review", "needs_changes", "--attempt-id", old)
        new = second["nodes"]["review"]["attempt_id"]
        self.assertNotEqual(old, new)
        stale = cli("review", run_id, "review", "approved", "--attempt-id", old, code=1)
        self.assertIn("detail", stale)
        self.assertEqual(cli("status", run_id)["status"], "waiting")
        done = cli("review", run_id, "review", "approved", "--attempt-id", new)
        self.assertEqual(done["status"], "completed")

    def test_dag_join_still_waits_for_all_forward_parents(self):
        completed = []

        async def runner(node, run):
            if node["id"] == "join":
                self.assertEqual(set(completed), {"a", "b"})
            else:
                await asyncio.sleep(0.02 if node["id"] == "a" else 0.15)
                completed.append(node["id"])
            return Result(output="done")

        wf = workflow()
        wf["nodes"] = [
            {"id": "start", "type": "prompt"},
            *[{"id": name, "type": "agent_task", "prompt": name} for name in ("a", "b", "join")],
            {"id": "done", "type": "finish"},
        ]
        wf["edges"] = (
            [{"from": "start", "to": name} for name in ("a", "b")]
            + [{"from": name, "to": "join"} for name in ("a", "b")]
            + [{"from": "join", "to": "done"}]
        )
        runtime = self.runtime(runner)
        run_id = runtime.engine.start(wf)["run_id"]
        self.assertEqual(self.wait(run_id)["status"], "completed")
