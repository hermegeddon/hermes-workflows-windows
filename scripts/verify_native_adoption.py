"""Test adoption against installed Hermes in a temporary home, with no LLMs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from verify_native_host import activate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    activate(args.hermes_source)
    with tempfile.TemporaryDirectory(prefix="workflow native adoption ") as directory:
        home = Path(directory).resolve()
        os.environ.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home))
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
            os.environ.pop(key, None)
        (home / "config.yaml").write_text("model: test-only\n", encoding="utf-8")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
        from hermes_cli import kanban_db as native
        from hermes_cli import kanban_db_connect as store
        from hermes_workflows.db import Database
        from hermes_workflows.executor import Executor, Result
        from hermes_workflows.kanban import KanbanExecutor
        from hermes_workflows.native import NativeHost
        from hermes_workflows.runtime import Runtime
        from hermes_workflows.specs import SpecStore

        host = NativeHost(home)
        db = Database(home / "workflows/runs.db")
        with host.scope():
            native.create_board("qa-adopt")
            conn = store.connect(board="qa-adopt")
            try:
                ids = [
                    native.create_task(
                        conn,
                        title=f"Existing user card {n}",
                        body="Preserve this body",
                        created_by="user",
                        triage=True,
                        project_id="",
                        workspace_kind="dir",
                        workspace_path=str(home),
                        board="qa-adopt",
                    )
                    for n in range(3)
                ]
                native.link_tasks(conn, ids[0], ids[1])
            finally:
                conn.close()

        async def collect(node, run):
            return Result(output=f"Ignore t_notchosen.\n```task_ids\n{ids[1]}\n{ids[0]}\n```")

        def new_runtime():
            runtime = Runtime(
                db,
                SpecStore([home / "workflows/global"]),
                Executor(home=home, agent_runner=collect),
            )
            runtime.engine.kanban = KanbanExecutor(db, host, poll_seconds=0.02)
            return runtime.start()

        def card(identity):
            with host.scope():
                conn = store.connect(board="qa-adopt")
                try:
                    return native.get_task(conn, identity)
                finally:
                    conn.close()

        def wait(predicate, phase):
            deadline = time.monotonic() + 30
            while not predicate():
                if time.monotonic() >= deadline:
                    raise AssertionError(f"timeout: {phase}")
                time.sleep(0.03)

        def finish(identity, *, reviewer=False, summary):
            with host.scope():
                conn = store.connect(board="qa-adopt")
                try:
                    claim = native.claim_review_task if reviewer else native.claim_task
                    task = claim(conn, identity)
                    assert task is not None, (identity, card(identity).status)
                    assert native.complete_task(
                        conn,
                        identity,
                        summary=summary,
                        expected_run_id=task.current_run_id,
                        fire_lifecycle_hook=False,
                    )
                finally:
                    conn.close()

        wf = {
            "id": "native-adoption-proof",
            "name": "Native adoption proof",
            "version": 1,
            "scope": {"type": "project", "projects": ["qa-adopt"]},
            "trigger": {"type": "manual"},
            "defaults": {"profile": "default"},
            "nodes": [
                {"id": "collect", "type": "agent_task", "prompt": "No model call", "board": False},
                {
                    "id": "drive",
                    "type": "agent_task",
                    "prompt": "Adopt",
                    "adopt": True,
                    "task_ref": "{{nodes.collect.output.task_ids}}",
                    "review_profile": "reviewer",
                },
                {"id": "done", "type": "finish"},
            ],
            "edges": [
                {"from": "collect", "to": "drive"},
                {
                    "from": "drive",
                    "to": "done",
                    "condition": {"type": "node_status", "node": "drive", "equals": "success"},
                },
            ],
        }
        runtime = new_runtime()
        try:
            run = runtime.engine.start(wf)
            wait(lambda: card(ids[0]).status == "ready", "first prerequisite adopted")
            assert card(ids[1]).status == "triage"
            finish(ids[0], summary="implementer first")
            wait(lambda: card(ids[0]).status == "review", "first review routed")
            assert card(ids[0]).assignee == "reviewer"
            runtime.close()
            finish(ids[0], reviewer=True, summary="REVIEW_FIRST_FINAL")
            runtime = new_runtime()
            wait(lambda: card(ids[1]).status == "ready", "dependent adopted after offline review")
            finish(ids[1], summary="implementer second")
            wait(lambda: card(ids[1]).status == "review", "second review routed")
            finish(ids[1], reviewer=True, summary="REVIEW_SECOND_FINAL")
            wait(
                lambda: db.get_run(run["run_id"])["status"] in ("completed", "failed"),
                "workflow completion",
            )
            final = db.get_run(run["run_id"])
            assert final["status"] == "completed", final
            assert final["nodes"]["drive"]["output"] == "REVIEW_FIRST_FINAL\n\nREVIEW_SECOND_FINAL"
            with host.scope():
                conn = store.connect(board="qa-adopt")
                try:
                    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3
                    assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 4
                    assert (
                        conn.execute(
                            "SELECT count(*) FROM task_events WHERE kind='review_requested'"
                        ).fetchone()[0]
                        == 2
                    )
                    assert all(
                        r[0] == "user" and r[1] == "Preserve this body"
                        for r in conn.execute("SELECT created_by,body FROM tasks")
                    )
                finally:
                    conn.close()
            assert card(ids[2]).status == "triage"
            cancel_wf = {
                **wf,
                "nodes": [{**wf["nodes"][1], "task_ref": ids[2]}, wf["nodes"][2]],
                "edges": wf["edges"][1:],
            }
            cancelled = runtime.engine.start(cancel_wf)
            wait(lambda: card(ids[2]).status == "ready", "cancellable adoption")
            runtime.engine.cancel(cancelled["run_id"])
            assert card(ids[2]).status == "ready"
            assert all(card(identity).status == "done" for identity in ids[:2])
            print(
                json.dumps(
                    {
                        "native_adoption_identity": "passed",
                        "dependency_order": "passed",
                        "native_review_claims": "passed",
                        "offline_review_recovery": "passed",
                        "cancel_preserves_user_card": "passed",
                        "cards_created_by_plugin": 0,
                        "model_calls": 0,
                    },
                    indent=2,
                )
            )
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
