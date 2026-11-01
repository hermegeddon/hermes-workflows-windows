from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.db import Database
from hermes_workflows.executor import Executor, Result
from hermes_workflows.kanban import KanbanExecutor
from hermes_workflows.native import NativeHost
from hermes_workflows.runtime import Runtime
from hermes_workflows.specs import SpecStore, validate
from hermes_workflows.worktrees import WorktreeError, Worktrees, repository_lock
from test_adoption import AdoptionBoard, workflow


def git(repo, *args, timeout=10):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # These are disposable, locally authored repositories. No hooks, prompts,
    # signing, fsmonitor or inherited repository selection belong in a fixture.
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + str(Path(repo) / ".workflow-disabled-hooks"),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


class WorktreeBoard(AdoptionBoard):
    @staticmethod
    def _schema(conn):
        AdoptionBoard._schema(conn)
        for column in ("workspace_kind", "workspace_path", "branch_name"):
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")

    def host(self, profile="default"):
        parent = super().host(profile)

        def loader(name):
            if name == "hermes_cli.kanban_db_workspace":
                return SimpleNamespace(_git=git)
            return parent.loader(name)

        return NativeHost(parent.home, loader=loader)

    def read_board_metadata(self, board):
        return {"default_workdir": str(self.repo)}

    def create_task(self, conn, **kwargs):
        identity = super().create_task(conn, **kwargs)
        conn.execute(
            "UPDATE tasks SET workspace_kind=?,workspace_path=?,branch_name=? WHERE id=?",
            (
                kwargs.get("workspace_kind"),
                kwargs.get("workspace_path"),
                kwargs.get("branch_name"),
                identity,
            ),
        )
        return identity


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow trees żółć ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "Project źródło with spaces"
        self.repo.mkdir()
        self.run_git(self.repo, "init", "-q", "-b", "main")
        self.run_git(self.repo, "config", "user.email", "qa@example.invalid")
        self.run_git(self.repo, "config", "user.name", "Workflow QA")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.commit(self.repo, "base")
        self.run_git(self.repo, "checkout", "-q", "-b", "feat/release")
        (self.repo / "release.txt").write_text("release seed\n", encoding="utf-8")
        self.seed = self.commit(self.repo, "release seed")
        self.board = WorktreeBoard(self.root / "native")
        self.board.repo = self.repo
        self.board.create_board("demo")
        self.host = self.board.host()
        self.trees = Worktrees(self.host)
        self.context = self.trees.context({}, self.board, "demo")

    def run_git(self, repo, *args):
        result = git(repo, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, repo, message):
        self.run_git(repo, "add", "--", ".", ":(exclude).worktrees")
        self.run_git(repo, "commit", "-qm", message)
        return self.run_git(repo, "rev-parse", "HEAD")

    def workspace(self, identity="t_first"):
        workspace = self.trees.target(self.context, identity)
        self.trees.materialize(self.context, workspace)
        return workspace

    def wait(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail(
            "worktree fixture timed out: "
            + json.dumps(
                {
                    "cards": self.board.cards(),
                    "runs": self.db.list_runs("all") if hasattr(self, "db") else [],
                }
            )
        )

    def runtime(self, ids):
        self.db = Database(self.root / "runs.db")

        async def collect(node, run):
            return Result(output="```task_ids\n" + "\n".join(ids) + "\n```")

        runtime = Runtime(self.db, SpecStore([self.root / "specs"]), Executor(agent_runner=collect))
        runtime.engine.kanban = KanbanExecutor(self.db, self.host, poll_seconds=0.02)
        self.addCleanup(runtime.close)
        return runtime.start()

    def test_two_cards_physically_build_on_shared_branch(self):
        first, second = self.board.card(), self.board.card()
        self.board.link(first, second)
        runtime = self.runtime([second, first])
        wf = workflow("{{nodes.collect.output.task_ids}}", stack=True)
        wf["nodes"].insert(
            0, {"id": "collect", "type": "agent_task", "prompt": "find", "board": False}
        )
        wf["edges"].insert(0, {"from": "collect", "to": "drive"})
        run = runtime.engine.start(wf)
        snapshots = []
        for identity, filename in ((first, "first.txt"), (second, "second.txt")):
            card = self.wait(
                lambda identity=identity: next(
                    (
                        c
                        for c in self.board.cards()
                        if c["id"] == identity and c["status"] == "ready"
                    ),
                    None,
                )
            )
            path = Path(card["workspace_path"])
            self.assertEqual(path, self.repo / ".worktrees" / identity)
            self.assertEqual(card["branch_name"], "wf/" + identity)
            self.assertTrue((path / "release.txt").exists())
            if identity == second:
                self.assertTrue((path / "first.txt").exists())
                self.assertEqual(self.run_git(path, "rev-parse", "HEAD"), snapshots[0])
            (path / filename).write_text(identity, encoding="utf-8")
            snapshots.append(self.commit(path, filename))
            self.board.finish(identity, output="done")
        final = self.wait(
            lambda: s if (s := self.db.get_run(run["run_id"]))["status"] == "completed" else None
        )
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), snapshots[-1])
        plan = final["nodes"]["drive"]["adoption"]
        self.assertEqual(plan["done"], [first, second])
        self.assertTrue(all(b["status"] == "completed" for b in plan["barriers"].values()))
        self.assertEqual(len(self.board.cards()), 2)

    def test_regular_native_worktree_is_materialized_before_dispatch(self):
        runtime = self.runtime([])
        wf = workflow("t_unused")
        wf["nodes"][0].pop("adopt")
        wf["nodes"][0].pop("task_ref")
        wf["nodes"][0]["workspace"] = {"type": "worktree"}
        run = runtime.engine.start(wf)
        card = self.wait(
            lambda: next((c for c in self.board.cards() if c["status"] == "ready"), None)
        )
        path = Path(card["workspace_path"])
        self.assertTrue((path / ".git").is_file())
        self.assertEqual(self.run_git(path, "rev-parse", "HEAD"), self.seed)
        runtime.engine.cancel(run["run_id"])
        self.wait(lambda: self.board.archived)
        self.assertTrue(path.exists())  # This double doesn't own native cleanup.

    def test_replay_after_process_death_between_fast_forward_and_ack(self):
        workspace = self.workspace()
        path = Path(workspace["path"])
        (path / "impl.txt").write_text("implementation", encoding="utf-8")
        after = self.commit(path, "implementation")
        intent = self.trees.barrier_intent(self.context, workspace)
        receipt = self.root / "barrier.json"
        receipt.write_text(json.dumps([self.context, intent]), encoding="utf-8")
        # Commit identities are durable before the child changes Git. Death
        # leaves no ack; replay must recognize the exact post-merge HEAD.
        code = """import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from test_worktrees import git
from hermes_workflows.worktrees import Worktrees,repository_lock
from types import SimpleNamespace
class Host:
 def call(self,feature,operation,*args,**kwargs): return git(*args,**kwargs)
context,intent=json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))
Worktrees(Host()).advance(context,intent)
with repository_lock(context['common']): os._exit(73)
"""
        child = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                code,
                str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"),
                str(Path(__file__).parent),
                str(receipt),
            ],
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual(self.trees.advance(self.context, intent), after)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), after)
        with repository_lock(self.context["common"]):
            pass  # Child death did not leave a stale logical lock.

    def test_dirty_root_card_and_wrong_branch_preserve_work(self):
        workspace = self.workspace()
        path = Path(workspace["path"])
        (path / "uncommitted.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(WorktreeError, "Uncommitted"):
            self.trees.barrier_intent(self.context, workspace)
        self.assertEqual((path / "uncommitted.txt").read_text(), "keep")
        self.commit(path, "keep user changes")
        (self.repo / "operator.txt").write_text("also keep", encoding="utf-8")
        with self.assertRaisesRegex(WorktreeError, "Uncommitted"):
            self.trees.barrier_intent(self.context, workspace)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), self.seed)
        (self.repo / "operator.txt").unlink()
        self.run_git(self.repo, "checkout", "-q", "main")
        with self.assertRaisesRegex(WorktreeError, "expected shared branch"):
            self.trees.barrier_intent(self.context, workspace)
        self.assertEqual(self.run_git(self.repo, "branch", "--show-current"), "main")

    def test_non_fast_forward_and_occupied_target_are_rejected(self):
        workspace = self.workspace()
        path = Path(workspace["path"])
        self.run_git(path, "checkout", "--detach", "main")
        with self.assertRaises(WorktreeError):
            self.trees.barrier_intent(self.context, workspace)
        other = self.trees.target(self.context, "t_occupied")
        Path(other["path"]).mkdir()
        (Path(other["path"]) / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(WorktreeError):
            self.trees.materialize(self.context, other)
        self.assertEqual((Path(other["path"]) / "keep.txt").read_text(), "keep")

    def test_divergent_commits_and_changed_card_branch_stop_barrier(self):
        workspace = self.workspace()
        path = Path(workspace["path"])
        (path / "card.txt").write_text("card", encoding="utf-8")
        self.commit(path, "card")
        (self.repo / "external.txt").write_text("external", encoding="utf-8")
        external = self.commit(self.repo, "external")
        self.context["tip"] = external
        with self.assertRaisesRegex(WorktreeError, "divergent"):
            self.trees.barrier_intent(self.context, workspace)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), external)
        self.run_git(path, "checkout", "-q", "-b", "operator-branch")
        with self.assertRaisesRegex(WorktreeError, "branch changed"):
            self.trees.verify_workspace(self.context, workspace)

    def test_worktree_setup_recovers_lost_ack_without_a_second_card(self):
        actual = Worktrees.materialize
        calls = []

        def lose_ack(manager, context, workspace):
            actual(manager, context, workspace)
            calls.append(workspace["path"])
            if len(calls) == 1:
                raise asyncio.CancelledError("driver stopped after filesystem commit")

        with patch.object(Worktrees, "materialize", lose_ack):
            runtime = self.runtime([])
            wf = workflow("t_unused")
            wf["nodes"][0].pop("adopt")
            wf["nodes"][0].pop("task_ref")
            wf["nodes"][0]["workspace"] = {"type": "worktree"}
            with patch("hermes_workflows.runtime.log.exception"):
                run = runtime.engine.start(wf)
                self.wait(lambda: calls)
                runtime.close()
            runtime = self.runtime([])
            card = self.wait(
                lambda: next((c for c in self.board.cards() if c["status"] == "ready"), None)
            )
            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(len(set(calls)), 1)
            self.assertEqual(len(self.board.cards()), 1)
            self.board.finish(card["id"], output="done")
            self.wait(lambda: self.db.get_run(run["run_id"])["status"] == "completed")

    def collected_stack(self, identities, **options):
        runtime = self.runtime(identities)
        wf = workflow("{{nodes.collect.output.task_ids}}", stack=True, **options)
        wf["nodes"].insert(
            0, {"id": "collect", "type": "agent_task", "prompt": "find", "board": False}
        )
        wf["edges"].insert(0, {"from": "collect", "to": "drive"})
        return runtime.engine.start(wf)

    def test_failed_or_dirty_stacked_card_never_promotes_the_next(self):
        first, second = self.board.card(), self.board.card()
        run = self.collected_stack([first, second])
        card = self.wait(
            lambda: next(
                (c for c in self.board.cards() if c["id"] == first and c["status"] == "ready"), None
            )
        )
        path = Path(card["workspace_path"])
        (path / "uncommitted.txt").write_text("preserve", encoding="utf-8")
        self.board.finish(first, output="done")
        state = self.wait(
            lambda: s if (s := self.db.get_run(run["run_id"]))["status"] == "failed" else None
        )
        self.assertTrue(state["nodes"]["drive"]["abort_run"])
        self.assertEqual(
            next(c for c in self.board.cards() if c["id"] == second)["status"], "triage"
        )
        self.assertEqual((path / "uncommitted.txt").read_text(), "preserve")
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), self.seed)

    def test_native_review_keeps_workspace_and_includes_reviewer_commit(self):
        identity = self.board.card()
        run = self.collected_stack([identity], review_profile="qa")
        card = self.wait(
            lambda: next(
                (c for c in self.board.cards() if c["id"] == identity and c["status"] == "ready"),
                None,
            )
        )
        path = Path(card["workspace_path"])
        (path / "impl.txt").write_text("implementation", encoding="utf-8")
        self.commit(path, "implementation")
        self.board.finish(identity, output="implemented")
        reviewed = self.wait(
            lambda: next(
                (c for c in self.board.cards() if c["id"] == identity and c["status"] == "review"),
                None,
            )
        )
        self.assertEqual(reviewed["workspace_path"], str(path))
        (path / "review.txt").write_text("reviewed", encoding="utf-8")
        after = self.commit(path, "review")
        self.board.finish(identity, output="reviewed")
        self.wait(lambda: self.db.get_run(run["run_id"])["status"] == "completed")
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), after)

    def test_retry_merges_completed_card_branch_and_accepts_already_merged_card(self):
        identity = self.board.card()
        runtime = self.runtime([])
        wf = workflow(identity, stack=True)
        run = runtime.engine.start(wf)
        card = self.wait(
            lambda: next(
                (c for c in self.board.cards() if c["id"] == identity and c["status"] == "ready"),
                None,
            )
        )
        path = Path(card["workspace_path"])
        (path / "impl.txt").write_text("implementation", encoding="utf-8")
        after = self.commit(path, "implementation")
        pending = self.repo / "operator.txt"
        pending.write_text("operator edits", encoding="utf-8")
        self.board.finish(identity, output="done")
        self.wait(lambda: self.db.get_run(run["run_id"])["status"] == "failed")
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), self.seed)
        pending.unlink()  # Only this test-authored sentinel, never production cleanup.
        runtime.engine.retry(run["run_id"], wf, "drive")
        self.wait(lambda: self.db.get_run(run["run_id"])["status"] == "completed")
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), after)
        again = runtime.engine.start(wf)
        self.wait(lambda: self.db.get_run(again["run_id"])["status"] == "completed")
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), after)
        self.assertEqual(len(self.board.cards()), 1)

    def test_failed_native_card_stops_the_stack_before_next_promotion(self):
        first, second = self.board.card(), self.board.card()
        run = self.collected_stack([first, second])
        self.wait(
            lambda: next(
                (c for c in self.board.cards() if c["id"] == first and c["status"] == "ready"), None
            )
        )
        self.board.finish(first, outcome="failed", output="worker failed")
        self.wait(lambda: self.db.get_run(run["run_id"])["status"] == "failed")
        self.assertEqual(
            next(c for c in self.board.cards() if c["id"] == second)["status"], "triage"
        )
        self.assertFalse((self.repo / ".worktrees" / second).exists())

    @unittest.skipUnless(os.name == "nt", "Windows sharing lock")
    def test_windows_locked_file_barrier_can_be_retried(self):
        import ctypes
        from ctypes import wintypes

        workspace = self.workspace()
        path = Path(workspace["path"])
        (path / "base.txt").write_text("changed\n", encoding="utf-8")
        after = self.commit(path, "change locked target")
        intent = self.trees.barrier_intent(self.context, workspace)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.repo / "base.txt"), 0x80000000, 0, None, 3, 128, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(WorktreeError):
                self.trees.advance(self.context, intent)
        finally:
            kernel.CloseHandle(handle)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD"), self.seed)
        self.assertEqual(self.trees.advance(self.context, intent), after)

    def test_context_rejects_relative_paths_dirty_checkout_and_hermes_anchor(self):
        with self.assertRaisesRegex(WorktreeError, "absolute"):
            self.trees.context({"workdir": "relative"}, self.board, "demo")
        (self.repo / "untracked.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(WorktreeError, "Uncommitted"):
            self.trees.context({}, self.board, "demo")
        original = self.host.loader
        with patch.object(
            self.host,
            "loader",
            side_effect=lambda n: (
                SimpleNamespace(__file__=str(self.repo / "hermes_cli/__init__.py"))
                if n == "hermes_cli"
                else original(n)
            ),
        ):
            with self.assertRaisesRegex(WorktreeError, "Hermes code"):
                self.trees.context({}, self.board, "demo")

    def test_validation_does_not_silently_ignore_stack_and_offboard_worktrees(self):
        self.assertTrue(validate(workflow("t_a", stack=True))["valid"])
        for options in (
            {"stack": "yes"},
            {"workspace": {"type": "unknown"}},
            {"workspace": {"type": "worktree"}, "board": False},
            {"branch": "feat/x"},
        ):
            self.assertFalse(validate(workflow("t_a", **options))["valid"])


if __name__ == "__main__":
    unittest.main()
