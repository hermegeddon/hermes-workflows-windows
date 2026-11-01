from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-workflows-py"
sys.path.insert(0, str(PLUGIN))
from hermes_workflows import _command, _handler
from hermes_workflows.api import API
from hermes_workflows.commands import (
    parse_resume_args,
    parse_run_args,
    resolve_nl_command,
    tokenize,
)
from hermes_workflows.db import ConflictError, Database
from hermes_workflows.engine import Engine
from hermes_workflows.executor import Executor, Result
from hermes_workflows.kanban import KanbanExecutor
from hermes_workflows.runtime import Runtime
from hermes_workflows.specs import SpecStore, compile_preview
from test_kanban import BoardDouble

REFERENCE = json.loads(
    (Path(__file__).parent / "fixtures/command-reference.json").read_text(encoding="utf-8")
)


def workflow():
    return {
        "id": "resume-qa",
        "name": "Resume QA",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "qa"},
        "nodes": [
            {"id": "a", "type": "agent_task", "prompt": "prefix"},
            {"id": "b", "type": "agent_task", "prompt": "broken"},
            {"id": "end", "type": "finish"},
        ],
        "edges": [
            {"from": "a", "to": "b"},
            {
                "from": "b",
                "to": "end",
                "condition": {"type": "node_status", "node": "b", "equals": "success"},
            },
        ],
    }


class CommandsResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow resume żółć ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = Database(self.home / "workflows/runs.db")
        self.db.save_settings({"scripts_enabled": True})
        self.store = SpecStore([self.home / "workflows/global"])
        self.engine = Engine(self.db)

    def wait(self, runtime, run_id, status):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.db.get_run(run_id)
            if state["status"] == status:
                return state
            time.sleep(0.02)
        self.fail(f"expected {status}: {self.db.get_run(run_id)}; {self.db.trace(run_id)}")

    def seeded(self, wf=None, *, name="seed"):
        wf = wf or workflow()
        state = self.engine.start(wf, run_id=name)

        def fail(run):
            run.update(status="failed", finished_at=time.time())
            run["nodes"]["a"].update(
                status="completed", outcome="success", output="prefix kept", seq=1
            )
            run["nodes"]["b"].update(status="completed", outcome="failure", error="fix me", seq=2)
            run["nodes"]["end"].update(status="skipped", seq=3)

        return self.db.mutate_run(state["run_id"], fail)

    def test_frozen_upstream_resume_decisions(self):
        original = REFERENCE["resumes"][0]["workflow"]
        for case in REFERENCE["resumes"]:
            with self.subTest(case["name"]):
                state = self.engine.start(original, run_id=case["name"])

                def seed(run, case=case):
                    run.update(
                        status=case["run"]["status"], nodes=copy.deepcopy(case["run"]["nodes"])
                    )

                before = self.db.mutate_run(state["run_id"], seed)
                history = self.db.attempts(state["run_id"])
                if case["expected"].get("refused"):
                    with self.assertRaises(ValueError):
                        self.engine.resume(
                            state["run_id"],
                            case["workflow"],
                            node=case["node"],
                            reset_all=case["all"],
                        )
                    self.assertEqual(self.db.get_run(state["run_id"]), before)
                    self.assertEqual(self.db.attempts(state["run_id"]), history)
                    if before["status"] in ("created", "running", "waiting"):
                        self.engine.cancel(state["run_id"])
                    continue
                after = self.engine.resume(
                    state["run_id"], case["workflow"], node=case["node"], reset_all=case["all"]
                )
                if case["all"]:
                    self.assertTrue(all(n["status"] == "pending" for n in after["nodes"].values()))
                else:
                    self.assertEqual(
                        after["nodes"][case["expected"]["target"]]["status"], "pending"
                    )
                    self.assertEqual(after["nodes"]["a"], before["nodes"]["a"])
                self.assertEqual(self.db.get_spec(state["run_id"]), case["workflow"])
                self.assertEqual(self.db.attempts(state["run_id"]), history)
                self.engine.cancel(state["run_id"])

    def test_real_failure_resume_uses_live_prompt_preserves_prefix_and_history(self):
        calls = []

        async def runner(node, run):
            calls.append((node["id"], node["prompt"]))
            return (
                Result("failure", error="broken")
                if node["prompt"].endswith("broken")
                else Result(output=node["prompt"])
            )

        wf = workflow()
        self.store.save({"workflow": wf}, create=True)
        runtime = Runtime(self.db, self.store, Executor(agent_runner=runner)).start()
        self.addCleanup(runtime.close)
        run = runtime.engine.start(wf, input="operator input", params={})
        before = self.wait(runtime, run["run_id"], "failed")
        old_attempt = before["nodes"]["b"]["attempt_id"]
        wf["nodes"][1]["prompt"] = "fixed"
        wf["version"] = 2
        self.store.save({"workflow": wf})
        code, result = API(runtime).handle("POST", f"/runs/{run['run_id']}/resume")
        self.assertEqual(code, 200, result)
        final = self.wait(runtime, run["run_id"], "completed")
        self.assertEqual(final["nodes"]["a"], before["nodes"]["a"])
        self.assertNotEqual(final["nodes"]["b"]["attempt_id"], old_attempt)
        self.assertEqual(final["nodes"]["b"]["attempt"], 2)
        self.assertEqual([n for n, _ in calls], ["a", "b", "b"])
        self.assertTrue(calls[-1][1].endswith("fixed"))
        self.assertEqual(final["input"], "operator input")
        self.assertEqual(final["workflow_version"], 2)
        with self.assertRaises(ConflictError):
            self.db.mutate_run(
                run["run_id"], lambda r: None, revision=0, attempt=("b", old_attempt)
            )

    def test_resume_sibling_and_concurrent_conflicts_rollback(self):
        before = self.seeded()
        sibling = self.engine.start(workflow())
        history = self.db.attempts(before["run_id"])
        with self.assertRaises(ConflictError):
            self.engine.resume(before["run_id"], workflow())
        self.assertEqual(self.db.get_run(before["run_id"]), before)
        self.assertEqual(self.db.attempts(before["run_id"]), history)
        self.engine.cancel(sibling["run_id"])

        def resume(_):
            try:
                self.engine.resume(before["run_id"], workflow())
                return "ok"
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(resume, [0, 1]), ["ok", "conflict"])

    def test_resume_rejects_bad_targets_and_does_not_select_routing_failure(self):
        before = self.seeded()
        for kwargs in (
            {"node": "a"},
            {"node": "absent"},
            {"node": ""},
            {"node": "b", "reset_all": True},
            {"reset_all": "false"},
        ):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                self.engine.resume(before["run_id"], workflow(), **kwargs)
            self.assertEqual(self.db.get_run(before["run_id"]), before)
        wf = workflow()
        wf["nodes"][1] = {"id": "b", "type": "condition"}
        self.db.mutate_run(
            before["run_id"], lambda r: r["nodes"]["b"].update(node_type="condition"), spec=wf
        )
        with self.assertRaisesRegex(ConflictError, "no failed node"):
            self.engine.resume(before["run_id"], wf)

    def test_resume_keeps_workflow_identity_and_active_run_capacity(self):
        before = self.seeded()
        other = workflow()
        other["id"] = "another-workflow"
        with self.assertRaisesRegex(ValueError, "workflow id"):
            self.engine.resume(before["run_id"], other)
        self.db.save_settings({"max_parallel_runs": 1})
        sibling = self.engine.start(other)
        with self.assertRaisesRegex(ConflictError, "active run limit"):
            self.engine.resume(before["run_id"], workflow())
        self.assertEqual(self.db.get_run(before["run_id"]), before)
        self.engine.cancel(sibling["run_id"])
        self.assertEqual(self.engine.resume(before["run_id"], workflow())["status"], "running")

    def test_retry_compatibility_and_resume_edge_edit_guard_are_distinct(self):
        before = self.seeded()
        edited = workflow()
        edited["edges"][-1] = {"from": "a", "to": "end"}
        with self.assertRaisesRegex(ConflictError, "graph changed"):
            self.engine.retry(before["run_id"], edited)
        self.assertEqual(
            self.engine.resume(before["run_id"], edited)["nodes"]["a"], before["nodes"]["a"]
        )
        self.engine.cancel(before["run_id"])
        restarted = self.engine.retry(before["run_id"], edited)
        self.assertTrue(all(n["status"] == "pending" for n in restarted["nodes"].values()))

    def test_native_resume_keeps_prefix_and_creates_one_new_owned_card(self):
        board = BoardDouble(self.home / "native")
        board.create_board("demo")
        wf = workflow()
        wf["scope"] = {"type": "project", "projects": ["demo"]}
        wf["nodes"][0] = {"id": "a", "type": "condition"}
        runtime = Runtime(self.db, self.store)
        runtime.engine.kanban = KanbanExecutor(self.db, board.host(), poll_seconds=0.01)
        self.addCleanup(runtime.close)
        runtime.start()
        run = runtime.engine.start(wf)

        def card_after(count):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                cards = board.cards()
                if len(cards) > count:
                    return cards[-1]
                time.sleep(0.02)
            self.fail(f"no native card: {self.db.get_run(run['run_id'])}")

        first = card_after(0)
        board.finish(first["id"], outcome="failed", output="first native failure")
        before = self.wait(runtime, run["run_id"], "failed")
        runtime.engine.resume(run["run_id"], wf)
        second = card_after(1)
        self.assertNotEqual(first["id"], second["id"])
        board.finish(second["id"], output="resumed native result")
        final = self.wait(runtime, run["run_id"], "completed")
        self.assertEqual(final["nodes"]["a"], before["nodes"]["a"])
        self.assertEqual(final["nodes"]["b"]["output"], "resumed native result")
        attempts = [a for a in self.db.attempts(run["run_id"]) if a["node_id"] == "b"]
        self.assertEqual([a["handle"]["task_id"] for a in attempts], [first["id"], second["id"]])
        self.assertEqual(len(board.cards()), 2)

    def test_resume_inside_review_loop_requires_a_fresh_gate(self):
        calls = []

        async def runner(node, run):
            calls.append(node["id"])
            return (
                Result("failure", error="second iteration failed")
                if len(calls) == 2
                else Result(output="ok")
            )

        wf = workflow()
        wf["nodes"] = [
            {"id": "prefix", "type": "condition"},
            {"id": "work", "type": "agent_task", "prompt": "work"},
            {"id": "review", "type": "human_review"},
            {"id": "end", "type": "finish"},
        ]
        wf["edges"] = [
            {"from": "prefix", "to": "work"},
            {
                "from": "work",
                "to": "review",
                "condition": {"type": "node_status", "node": "work", "equals": "success"},
            },
            {
                "from": "review",
                "to": "work",
                "condition": {"type": "review_status", "equals": "needs_changes"},
            },
            {
                "from": "review",
                "to": "end",
                "condition": {"type": "review_status", "equals": "approved"},
            },
        ]
        runtime = Runtime(self.db, self.store, Executor(agent_runner=runner)).start()
        self.addCleanup(runtime.close)
        run = runtime.engine.start(wf)
        first = self.wait(runtime, run["run_id"], "waiting")
        runtime.engine.review(run["run_id"], "review", "needs_changes", "old note")
        before = self.wait(runtime, run["run_id"], "failed")
        runtime.engine.resume(run["run_id"], wf)
        fresh = self.wait(runtime, run["run_id"], "waiting")
        self.assertEqual(fresh["nodes"]["prefix"], before["nodes"]["prefix"])
        self.assertNotEqual(
            fresh["nodes"]["review"]["attempt_id"], first["nodes"]["review"]["attempt_id"]
        )
        self.assertNotIn("review_note", fresh["nodes"]["review"])
        self.assertNotIn("review_decision", fresh["nodes"]["review"])
        with self.assertRaises(ConflictError):
            runtime.engine.review(
                run["run_id"],
                "review",
                "approved",
                attempt_id=first["nodes"]["review"]["attempt_id"],
            )
        runtime.engine.review(
            run["run_id"], "review", "approved", attempt_id=fresh["nodes"]["review"]["attempt_id"]
        )
        self.wait(runtime, run["run_id"], "completed")
        self.assertEqual(len(calls), 3)

    def test_script_permission_and_resume_api_validation(self):
        wf = workflow()
        wf["nodes"][1] = {"id": "b", "type": "script", "command": "echo fixed"}
        before = self.seeded(wf)
        self.store.save({"workflow": wf}, create=True)
        runtime = Runtime(self.db, self.store)
        self.addCleanup(runtime.close)
        api = API(runtime)
        self.db.save_settings({"scripts_enabled": False})
        self.assertEqual(api.handle("POST", f"/runs/{before['run_id']}/resume")[0], 409)
        self.db.save_settings({"scripts_enabled": True})
        for payload in ({"all": "false"}, {"node": 3}, {"node": "b", "all": True}):
            self.assertEqual(
                api.handle("POST", f"/runs/{before['run_id']}/resume", payload)[0], 400
            )
        self.assertEqual(self.db.get_run(before["run_id"]), before)

    def test_frozen_natural_language_and_windows_command_parsing(self):
        for case in REFERENCE["natural_language"]:
            self.assertEqual(
                resolve_nl_command(case["text"], REFERENCE["catalog"]), case["expected"]
            )
        case = REFERENCE["ambiguous"]
        self.assertEqual(resolve_nl_command(case["text"], case["catalog"]), case["expected"])
        self.assertEqual(
            tokenize('run qa path="C:\\work space\\żółć"'),
            ["run", "qa", "path=C:\\work space\\żółć"],
        )
        self.assertEqual(
            parse_run_args(["demo", "count=3", "text=two words"]),
            ("demo", {"count": "3", "text": "two words"}),
        )
        for tokens in (["=bad"], ["demo", "stray"]):
            with self.assertRaises(ValueError):
                parse_run_args(tokens)
        self.assertEqual(parse_resume_args(["--node", "b"]), ("b", False))
        with self.assertRaises(ValueError):
            parse_resume_args(["--node", "b", "--all"])
        for case in REFERENCE["run_arguments"]:
            if case["expected"].get("refused"):
                with self.assertRaises(ValueError):
                    parse_run_args(case["tokens"])
            else:
                project, params = parse_run_args(case["tokens"])
                self.assertEqual({"project_id": project, "params": params}, case["expected"])

    def test_pinned_cli_default_argument_envelopes(self):
        from hermes_workflows import cli

        for case in REFERENCE["cli_arguments"]:
            with (
                self.subTest(case["tokens"]),
                patch.object(cli, "get_runtime", return_value=SimpleNamespace(close=lambda: None)),
                patch.object(cli, "API") as api,
                redirect_stdout(io.StringIO()),
            ):
                api.return_value.handle.return_value = (400, {"detail": "capture-only"})
                self.assertEqual(cli.main(case["tokens"]), 1)
                method, route, payload = api.return_value.handle.call_args.args
                expected = case["expected"]
                self.assertEqual(method, "POST")
                if expected["command"] == "resume":
                    self.assertEqual(route, f"/runs/{expected['run_id']}/resume")
                    self.assertEqual(payload, {"node": expected["node"], "all": expected["all"]})
                else:
                    self.assertEqual(
                        payload,
                        {
                            "project_id": expected["project"],
                            "input": expected["input"],
                            "params": json.loads(expected["params"]),
                        },
                    )
                    self.assertEqual(api.call_args.kwargs["origin"], expected["origin"])

    def test_slash_resume_nl_and_tool_keep_profile_origin_binding(self):
        before = self.seeded()
        self.store.save({"workflow": workflow()}, create=True)
        runtime = Runtime(self.db, self.store)
        self.addCleanup(runtime.close)
        with (
            patch("hermes_workflows.runtime.get_runtime", return_value=runtime),
            patch.object(runtime, "start", return_value=runtime),
        ):
            result = json.loads(_command(f"resume {before['run_id']}", home=self.home))
            self.assertEqual(result["nodes"]["a"], before["nodes"]["a"])
            self.assertEqual(
                json.loads(_handler("workflow_resume", self.home)({"run_id": before["run_id"]}))[
                    "status_code"
                ],
                409,
            )
            self.engine.cancel(before["run_id"])
            with (
                patch.object(runtime.origins, "origin_for", return_value="discord:fixture"),
                patch.object(
                    runtime.origins, "routing_for", return_value={"session_key": "fixture"}
                ),
            ):
                result = json.loads(
                    _command("Resume QA scope 3", home=self.home, session_key="fixture")
                )
            state = self.db.get_run(result["run_id"])
            self.assertEqual((state["origin"], state["input"]), ("discord:fixture", "scope 3"))
            self.assertIn("could not match", _command("unmatched workflow", home=self.home))
            self.assertIn("failed", _command("run resume-qa =bad", home=self.home))

    def test_invalid_tool_arguments_return_json_without_starting_a_runtime(self):
        with patch(
            "hermes_workflows.runtime.get_runtime", side_effect=AssertionError("unexpected startup")
        ):
            for args in ([], "bad", 3):
                result = json.loads(_handler("workflow_resume", self.home)(args))
                self.assertEqual(result, {"error": "tool arguments must be an object"})

    def test_modes_checkpoint_and_preview_match_actual_executor(self):
        async def run_mode(mode):
            self.db.save_settings({"default_mode": mode})
            wf = workflow()
            wf["id"] = mode
            wf["nodes"][:2] = [
                {"id": "a", "type": "script", "command": "one"},
                {"id": "b", "type": "script", "command": "two"},
            ]

            async def execute(node, run, config):
                return Result(output=node["command"])

            executor = Executor()
            executor.execute = execute
            engine = Engine(self.db, executor)
            run = engine.start(wf)
            self.assertTrue(self.db.claim(run["run_id"], mode))
            await engine.drive(run["run_id"], mode, checkpoint=True)
            return self.db.get_run(run["run_id"])

        durable = asyncio.run(run_mode("durable"))
        direct = asyncio.run(run_mode("direct"))
        self.assertEqual(durable["nodes"]["a"]["status"], "completed")
        self.assertEqual(durable["nodes"]["b"]["status"], "pending")
        self.assertEqual(direct["status"], "completed")
        wf = workflow()
        wf["scope"] = {"type": "project", "projects": ["demo"]}
        wf["nodes"][0]["board"] = False
        preview = compile_preview(wf, mode="direct")
        self.assertEqual(
            [task["executor"] for task in preview["kanban_tasks"]], ["direct", "kanban"]
        )
        self.assertTrue(preview["execution"]["inline_scripts"])

    def test_direct_script_drain_is_bounded_and_durable_checkpoint_can_restart(self):
        wf = workflow()
        wf["nodes"] = [
            {"id": f"s{i}", "type": "script", "command": "echo ok"} for i in range(65)
        ] + [{"id": "end", "type": "finish"}]
        wf["edges"] = [{"from": f"s{i}", "to": f"s{i + 1}"} for i in range(64)] + [
            {"from": "s64", "to": "end"}
        ]
        calls = []

        async def execute(node, run, config):
            calls.append(node["id"])
            return Result(output="ok")

        executor = Executor()
        executor.execute = execute
        engine = Engine(self.db, executor)
        run = engine.start(wf)

        async def advance(owner):
            self.assertTrue(self.db.claim(run["run_id"], owner))
            await engine.drive(run["run_id"], owner, checkpoint=True)
            return self.db.get_run(run["run_id"])

        first = asyncio.run(advance("durable"))
        self.assertEqual(calls, ["s0"])
        self.db.save_settings({"default_mode": "direct"})
        bounded = asyncio.run(advance("direct"))
        self.assertEqual(len(calls), 65)
        self.assertEqual(bounded["status"], "running")
        self.assertEqual(bounded["nodes"]["s0"], first["nodes"]["s0"])
        self.assertEqual(asyncio.run(advance("finish"))["status"], "completed")

    def test_direct_checkpoint_bound_also_applies_while_parallel_scripts_run(self):
        async def scenario():
            release = asyncio.Event()
            fast = []

            async def execute(node, run, config):
                if node["id"] == "slow":
                    await release.wait()
                else:
                    fast.append(node["id"])
                    if len(fast) == 64:
                        release.set()
                return Result(output="ok")

            wf = workflow()
            wf["nodes"] = [
                {"id": f"s{i}", "type": "script", "command": "echo ok"} for i in range(100)
            ] + [
                {"id": "start", "type": "condition"},
                {"id": "slow", "type": "script", "command": "echo slow"},
                {"id": "end", "type": "finish"},
            ]
            wf["edges"] = [{"from": f"s{i}", "to": f"s{i + 1}"} for i in range(99)] + [
                {"from": "start", "to": "s0"},
                {"from": "start", "to": "slow"},
                {"from": "s99", "to": "end"},
                {"from": "slow", "to": "end"},
            ]
            self.db.save_settings({"default_mode": "direct", "max_parallel_steps": 2})
            executor = Executor()
            executor.execute = execute
            engine = Engine(self.db, executor)
            run = engine.start(wf)
            self.db.claim(run["run_id"], "parallel")
            await asyncio.wait_for(engine.drive(run["run_id"], "parallel", checkpoint=True), 15)
            self.assertEqual(len(fast), 64)
            self.assertEqual(self.db.get_run(run["run_id"])["nodes"]["s64"]["status"], "pending")
            self.db.claim(run["run_id"], "remaining")
            await engine.drive(run["run_id"], "remaining", checkpoint=True)
            self.assertEqual(self.db.get_run(run["run_id"])["status"], "completed")

        asyncio.run(scenario())

    def test_cli_aliases_typed_params_and_resume_survive_process_exit(self):
        wf = workflow()
        wf["params"] = [{"name": "count", "type": "int", "default": 1}]
        wf["nodes"] = [
            {"id": "a", "type": "condition"},
            {"id": "b", "type": "human_review"},
            {"id": "end", "type": "finish"},
        ]
        wf["edges"] = [{"from": "a", "to": "b"}, {"from": "b", "to": "end"}]
        self.store.save({"workflow": wf}, create=True)
        env = {
            **os.environ,
            "PYTHONPATH": str(PLUGIN),
            "HERMES_HOME": str(self.home),
            "PYTHONUTF8": "1",
        }

        def cli(*args, code=0):
            result = subprocess.run(
                [sys.executable, "-m", "hermes_workflows", *args],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(result.returncode, code, result.stderr + result.stdout)
            return json.loads(result.stdout) if result.stdout.strip() else None

        run = cli(
            "run",
            wf["id"],
            "--params",
            '{"count":3}',
            "--origin",
            "discord:fixture",
            "--input",
            "żółć",
        )
        self.assertEqual(
            (run["params"]["count"], run["input"], run["origin"]), (3, "żółć", "discord:fixture")
        )
        self.assertEqual(
            cli("resume", run["run_id"], code=1)["detail"].split(",")[0], "run is waiting"
        )
        cli("cancel", run["run_id"])
        restarted = cli("resume", run["run_id"], "--all")
        self.assertEqual(restarted["status"], "waiting")
        self.assertEqual(cli("review", run["run_id"], "b", "approved")["status"], "completed")

    def test_cli_advance_run_is_scoped_and_advance_all_keeps_its_envelope(self):
        wf = workflow()
        wf["nodes"] = [{"id": "done", "type": "finish"}]
        wf["edges"] = []
        target = self.engine.start(wf)
        other = copy.deepcopy(wf)
        other["id"] = "unrelated"
        sibling = self.engine.start(other)
        env = {
            **os.environ,
            "PYTHONPATH": str(PLUGIN),
            "HERMES_HOME": str(self.home),
            "PYTHONUTF8": "1",
        }

        def cli(*args):
            result = subprocess.run(
                [sys.executable, "-m", "hermes_workflows", *args],
                cwd=self.home,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        self.db.claim(target["run_id"], "external-owner")
        self.assertEqual(cli("advance-run", target["run_id"])["status"], "created")
        self.db.execution(target["run_id"], owner="external-owner")
        self.assertEqual(self.db.get_run(sibling["run_id"]), sibling)
        self.db.release(target["run_id"], "external-owner")
        self.assertEqual(cli("advance-run", target["run_id"])["status"], "completed")
        self.assertEqual(self.db.get_run(sibling["run_id"]), sibling)
        result = cli("advance-all")
        self.assertEqual(
            result,
            {"advanced": [self.db.get_run(sibling["run_id"])], "dispatched": [], "active": False},
        )
        self.assertEqual(cli("advance-all"), {"advanced": [], "dispatched": [], "active": False})


class GitHubWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_gh_argv_executes_with_unicode_cwd_without_a_shell(self):
        from hermes_workflows.executor import run_process

        with tempfile.TemporaryDirectory(prefix="workflow gh żółć ") as temp:
            ref = "feature/żółć with spaces"

            async def shim(argv, **kwargs):
                program = (
                    "import json,os,sys;assert sys.argv[1:]=="
                    + repr(["pr", "view", ref, "--json", "state"])
                    + ";print(json.dumps({'state':'MERGED'}))"
                )
                return await run_process([sys.executable, "-c", program, *argv[1:]], **kwargs)

            with (
                patch("hermes_workflows.executor.shutil.which", return_value="gh"),
                patch("hermes_workflows.executor.run_process", side_effect=shim),
            ):
                result = await Executor._github_wait(
                    {"wait_for": {"github_pr_merged": ref}, "workdir": temp}
                )
            self.assertEqual((result.outcome, result.output), ("success", ref))

    async def test_numeric_branch_and_url_refs_use_argv_and_workdir(self):
        for ref in ("123", "feature/żółć", "https://github.com/owner/repo/pull/4"):
            run = AsyncMock(return_value=Result(output='{"state":"MERGED"}'))
            with (
                patch(
                    "hermes_workflows.executor.shutil.which",
                    return_value="C:/Program Files/GitHub CLI/gh.exe",
                ),
                patch("hermes_workflows.executor.run_process", run),
            ):
                result = await Executor._github_wait(
                    {"wait_for": {"github_pr_merged": ref}, "workdir": "C:/work space/żółć"}
                )
            self.assertEqual(result.outcome, "success")
            self.assertEqual(run.call_args.args[0][1:], ["pr", "view", ref, "--json", "state"])
            self.assertEqual(run.call_args.kwargs["cwd"], "C:/work space/żółć")
            self.assertEqual(run.call_args.kwargs["timeout"], 30)

    async def test_cli_transient_errors_and_pr_states(self):
        cases = [
            (Result(output=json.dumps({"state": case["state"]})), case["expected"])
            for case in REFERENCE["pr_states"]
        ]
        cases += [
            (Result(output="bad json"), None),
            (Result("failure", error="auth"), None),
        ]
        for response, expected in cases:
            with (
                patch("hermes_workflows.executor.shutil.which", return_value="gh"),
                patch("hermes_workflows.executor.run_process", AsyncMock(return_value=response)),
            ):
                result = await Executor._github_wait({"wait_for": {"github_pr_merged": "12"}})
            self.assertEqual(result.outcome if result else None, expected)

    async def test_no_gh_url_fallback_and_missing_context_retry(self):
        node = {"wait_for": {"github_pr_merged": "https://github.com/owner/repo/pull/1"}}
        with (
            patch("hermes_workflows.executor.shutil.which", return_value=None),
            patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"merged":true}')),
        ):
            self.assertEqual((await Executor._github_wait(node)).outcome, "success")
        with (
            patch("hermes_workflows.executor.shutil.which", return_value=None),
            patch("urllib.request.urlopen", side_effect=OSError("network")),
        ):
            self.assertIsNone(await Executor._github_wait(node))
            self.assertIsNone(await Executor._github_wait({"wait_for": {"github_pr_merged": "12"}}))
        for ref in ("", "--help", "12\n--help"):
            with self.assertRaises(ValueError):
                await Executor._github_wait({"wait_for": {"github_pr_merged": ref}})


if __name__ == "__main__":
    unittest.main()
