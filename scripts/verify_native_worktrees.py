"""Prove installed dispatcher worktree anchoring and stacking with no LLM workers."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

from verify_native_host import activate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    activate(args.hermes_source)
    with tempfile.TemporaryDirectory(prefix="workflow native worktrees żółć ") as temporary:
        home = Path(temporary).resolve()
        os.environ.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home))
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
            os.environ.pop(key, None)
        (home / "config.yaml").write_text("model: test-only\n", encoding="utf-8")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
        from hermes_cli import kanban_db as native
        from hermes_cli import kanban_db_connect as store
        from hermes_cli import kanban_db_dispatch as dispatch
        from hermes_workflows.db import Database
        from hermes_workflows.executor import Executor, Result
        from hermes_workflows.kanban import KanbanExecutor
        from hermes_workflows.native import NativeHost
        from hermes_workflows.runtime import Runtime
        from hermes_workflows.specs import SpecStore
        from hermes_workflows.worktrees import Worktrees

        host = NativeHost(home)
        trees = Worktrees(host)
        repo = home / "Project źródło with spaces"
        repo.mkdir()
        trees.git(repo, "init", "-q", "-b", "main")
        trees.git(repo, "config", "user.email", "qa@example.invalid")
        trees.git(repo, "config", "user.name", "Workflow QA")

        def commit(path, filename, text):
            (path / filename).write_text(text, encoding="utf-8")
            trees.git(path, "add", "--", filename)
            # Host hardening deliberately disables hooks; test commits also
            # disable any inherited signing policy, without changing user Git.
            trees.git(path, "-c", "commit.gpgsign=false", "commit", "-qm", filename)
            return trees.sha(path, "HEAD")

        commit(repo, "base.txt", "base")
        trees.git(repo, "checkout", "-q", "-b", "feat/native-proof")
        seed = commit(repo, "release.txt", "release seed")
        with host.scope():
            native.create_board("qa-stack")
            conn = store.connect(board="qa-stack")
            try:
                ids = [
                    native.create_task(
                        conn,
                        title=f"Existing card {n}",
                        body="Keep original instructions",
                        created_by="user",
                        triage=True,
                        project_id="",
                        board="qa-stack",
                    )
                    for n in range(2)
                ]
                native.link_tasks(conn, ids[0], ids[1])
            finally:
                conn.close()

        async def collect(node, run):
            return Result(output="```task_ids\n" + "\n".join(reversed(ids)) + "\n```")

        db = Database(home / "workflows/runs.db")

        def start_runtime():
            runtime = Runtime(
                db,
                SpecStore([home / "workflows/global"]),
                Executor(home=home, agent_runner=collect),
            )
            runtime.engine.kanban = KanbanExecutor(db, host, poll_seconds=0.05)
            return runtime.start()

        runtime = start_runtime()
        wf = {
            "id": "native-worktree-proof",
            "name": "Native worktree proof",
            "version": 1,
            "scope": {"type": "project", "projects": ["qa-stack"]},
            "trigger": {"type": "manual"},
            "defaults": {"profile": "default"},
            "nodes": [
                {"id": "collect", "type": "agent_task", "prompt": "fixture only", "board": False},
                {
                    "id": "drive",
                    "type": "agent_task",
                    "prompt": "adopt existing cards",
                    "adopt": True,
                    "task_ref": "{{nodes.collect.output.task_ids}}",
                    "stack": True,
                    "workdir": str(repo),
                },
                {"id": "done", "type": "finish"},
            ],
            "edges": [{"from": "collect", "to": "drive"}, {"from": "drive", "to": "done"}],
        }
        report = {"model_workers_launched": 0, "dispatches": []}
        try:
            run = runtime.engine.start(wf)

            def wait(predicate, phase, *, terminal_ok=False):
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    state = db.get_run(run["run_id"])
                    if not terminal_ok and state["status"] in ("failed", "cancelled"):
                        raise AssertionError({"phase": phase, "state": state})
                    result = predicate()
                    if result:
                        return result
                    time.sleep(0.05)
                raise AssertionError({"phase": phase, "state": state})

            def card(identity):
                with host.scope():
                    conn = store.connect(board="qa-stack")
                    try:
                        return native.get_task(conn, identity)
                    finally:
                        conn.close()

            commits = []
            for index, identity in enumerate(ids):
                wait(lambda identity=identity: card(identity).status == "ready", "card ready")
                if index == 1:
                    # Restart between cards; the persisted scope/branch tip is
                    # reused instead of re-resolving an incidental process CWD.
                    runtime.close()
                    runtime = start_runtime()
                with host.scope():
                    conn = store.connect(board="qa-stack")
                    captured = []
                    try:

                        def fake_spawn(
                            task, workspace, identity=identity, captured=captured, **kwargs
                        ):
                            path = Path(workspace).resolve()
                            assert path == repo / ".worktrees" / identity
                            assert trees.branch(path) == "wf/" + identity
                            assert trees.sha(path, "HEAD") == (commits[-1] if commits else seed)
                            if commits:
                                assert (path / "card-0.txt").is_file()
                            # Exercise the real native argv/env builder while
                            # intercepting its final spawn. Never start an agent.
                            proc = Mock(pid=0)
                            with (
                                patch.dict(os.environ, {"TERMINAL_CWD": str(args.hermes_source)}),
                                patch.object(
                                    dispatch.subprocess, "Popen", return_value=proc
                                ) as spawn,
                            ):
                                assert (
                                    dispatch._default_spawn(task, workspace, board="qa-stack") == 0
                                )
                                options = spawn.call_args.kwargs
                                assert options["cwd"] == workspace
                                assert options["env"]["TERMINAL_CWD"] == workspace
                                options["stdout"].close()
                            dispatch._live_worker_procs.pop(0, None)
                            captured.append((task, path))
                            return None

                        tick = dispatch.dispatch_once(
                            conn,
                            board="qa-stack",
                            spawn_fn=fake_spawn,
                            max_in_progress=1,
                            max_in_progress_per_profile=1,
                        )
                        assert len(tick.spawned) == 1 and len(captured) == 1, tick
                        task, path = captured[0]
                        commits.append(commit(path, f"card-{index}.txt", identity))
                        assert native.complete_task(
                            conn,
                            identity,
                            summary="completed fixture",
                            metadata={"node_outcome": "success"},
                            expected_run_id=task.current_run_id,
                            fire_lifecycle_hook=False,
                        )
                        report["dispatches"].append(
                            {"card": identity, "branch": task.branch_name, "cwd_pinned": True}
                        )
                    finally:
                        conn.close()
            wait(lambda: db.get_run(run["run_id"])["status"] == "completed", "workflow complete")
            assert trees.sha(repo, "HEAD") == commits[-1]
            assert trees.branch(repo) == "feat/native-proof"
            assert all(card(identity).created_by == "user" for identity in ids)
            report.update(
                shared_tip=commits[-1],
                cards_in_order=ids,
                restart="passed",
                fast_forward_barriers="passed",
            )
            cancel_spec = {
                **wf,
                "id": "native-worktree-cancel",
                "nodes": [
                    {
                        "id": "drive",
                        "type": "agent_task",
                        "prompt": "fixture only",
                        "workdir": str(repo),
                        "workspace": {"type": "worktree"},
                    },
                    {"id": "done", "type": "finish"},
                ],
                "edges": [{"from": "drive", "to": "done"}],
            }
            run = runtime.engine.start(cancel_spec)
            identity = wait(
                lambda: db.get_run(run["run_id"])["nodes"]["drive"].get("task_id"),
                "owned worktree ready",
            )
            path = Path(card(identity).workspace_path)
            (path / "operator.txt").write_text("preserve uncommitted user work", encoding="utf-8")
            runtime.engine.cancel(run["run_id"])
            wait(
                lambda: card(identity).status == "archived",
                "owned card cancellation cleanup",
                terminal_ok=True,
            )
            assert (path / "operator.txt").read_text(
                encoding="utf-8"
            ) == "preserve uncommitted user work"
            assert trees.branch(path) == "wf/" + identity
            report["native_cleanup_preserves_dirty_worktree"] = "passed"
        finally:
            runtime.close()
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
