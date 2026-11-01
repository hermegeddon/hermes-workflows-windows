"""Exercise installed native Kanban in a disposable home, with no model workers.

Run in its own process: the test-only environment overrides never affect a
gateway. The actual native dispatcher claims tasks using an injected spawn
callback; native complete_task records results without launching an LLM.
"""

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
    with tempfile.TemporaryDirectory(prefix="workflow native kanban ") as directory:
        home = Path(directory).resolve()
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_KANBAN_HOME"] = str(home)
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
            os.environ.pop(key, None)
        (home / "config.yaml").write_text("model: test-only\n", encoding="utf-8")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as store
        from hermes_cli import kanban_db_dispatch as dispatch
        from hermes_workflows.db import Database
        from hermes_workflows.kanban import KanbanExecutor
        from hermes_workflows.native import NativeHost
        from hermes_workflows.runtime import Runtime
        from hermes_workflows.specs import SpecStore

        db = Database(home / "workflows/runs.db")
        host = NativeHost(home)

        def new_runtime():
            runtime = Runtime(db, SpecStore([home / "workflows/global"]))
            runtime.engine.executor.home = home
            runtime.engine.kanban = KanbanExecutor(db, host, poll_seconds=0.05)
            return runtime.start()

        runtime = new_runtime()
        wf = {
            "id": "native-board-proof",
            "name": "Native board proof",
            "version": 1,
            "scope": {"type": "project", "projects": ["qa-board"]},
            "trigger": {"type": "manual"},
            "defaults": {"profile": "default"},
            "nodes": [
                {"id": "start", "type": "prompt"},
                *[
                    {
                        "id": n,
                        "type": "agent_task",
                        "prompt": "No real worker is launched",
                        "skills": [],
                    }
                    for n in ("a", "b")
                ],
                {"id": "done", "type": "finish"},
            ],
            "edges": [{"from": "start", "to": n} for n in ("a", "b")]
            + [{"from": n, "to": "done"} for n in ("a", "b")],
        }
        report = {}
        try:
            run = runtime.engine.start(wf)

            def wait_for(predicate, phase):
                # Dispatch may perform lazy host setup. Each asynchronous
                # phase needs its own budget and a useful last-state failure.
                deadline = time.monotonic() + 30
                while True:
                    state = db.get_run(run["run_id"])
                    if predicate(state):
                        return state
                    if (
                        state["status"] in ("completed", "failed", "cancelled")
                        or time.monotonic() >= deadline
                    ):
                        raise AssertionError(f"{phase}: {state}")
                    time.sleep(0.05)

            state = wait_for(
                lambda r: all(r["nodes"][n].get("task_id") for n in ("a", "b")),
                "waiting for native cards",
            )
            ids = [state["nodes"][n]["task_id"] for n in ("a", "b")]
            attempts = [state["nodes"][n]["attempt_id"] for n in ("a", "b")]
            runtime.close()
            runtime = new_runtime()
            with host.scope():
                conn = store.connect(board="qa-board")
                try:
                    assert Path(kb.kanban_db_path(board="qa-board")).is_relative_to(home)
                    claimed = []

                    def fake_spawn(task, workspace, **kwargs):
                        claimed.append(task.id)
                        return None

                    tick = dispatch.dispatch_once(
                        conn,
                        board="qa-board",
                        spawn_fn=fake_spawn,
                        max_in_progress=1,
                        max_in_progress_per_profile=1,
                    )
                    assert len(tick.spawned) == 1 and len(claimed) == 1
                    report["native_dispatch_cap"] = "passed"
                    for index in range(2):
                        task = kb.get_task(conn, claimed[index])
                        marker = f"NATIVE_KANBAN_FINAL_{index}"
                        assert kb.complete_task(
                            conn,
                            task.id,
                            result="stale result snapshot",
                            summary=marker,
                            metadata={"node_outcome": "success"},
                            expected_run_id=task.current_run_id,
                            fire_lifecycle_hook=False,
                        )
                        if index == 0:
                            dispatch.dispatch_once(
                                conn,
                                board="qa-board",
                                spawn_fn=fake_spawn,
                                max_in_progress=1,
                                max_in_progress_per_profile=1,
                            )
                    assert set(claimed) == set(ids)
                finally:
                    conn.close()
            final = wait_for(lambda r: r["status"] == "completed", "waiting for completion")
            assert final["status"] == "completed", final
            assert {final["nodes"][n]["output"] for n in ("a", "b")} == {
                "NATIVE_KANBAN_FINAL_0",
                "NATIVE_KANBAN_FINAL_1",
            }
            assert [final["nodes"][n]["attempt_id"] for n in ("a", "b")] == attempts
            assert [final["nodes"][n]["task_id"] for n in ("a", "b")] == ids
            with host.scope():
                conn = store.connect(board="qa-board")
                try:
                    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
                finally:
                    conn.close()
            report["native_restart_same_cards"] = "passed"

            # Exercise cleanup against the real archive API too, without
            # dispatching the second run's queued cards to any worker.
            run = runtime.engine.start(wf)
            queued = wait_for(
                lambda r: all(r["nodes"][n].get("task_id") for n in ("a", "b")),
                "waiting for cancellable cards",
            )
            runtime.engine.cancel(run["run_id"])
            with host.scope():
                conn = store.connect(board="qa-board")
                try:
                    assert all(
                        kb.get_task(conn, queued["nodes"][n]["task_id"]).status == "archived"
                        for n in ("a", "b")
                    )
                    assert all(kb.get_task(conn, task_id).status == "done" for task_id in ids)
                finally:
                    conn.close()
            report["native_cancel_owned_cards"] = "passed"
            report.update(
                native_card_creation="passed",
                native_final_summary="passed",
                native_claim_ownership="passed",
                model_workers_launched=0,
            )
        finally:
            runtime.close()
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
