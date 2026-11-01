"""Capture-only check of installed observer, worker callback and approval contracts.

No provider request, command execution, real gateway or external message is used.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from verify_native_host import activate


def verify(home):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as native_store
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import plugins
    from hermes_cli.approval_transport import ApprovalRequest, invoke_approval_transport
    from hermes_workflows.agent_worker import run_agent
    from hermes_workflows.api import API
    from hermes_workflows.db import ConflictError, Database
    from hermes_workflows.engine import Engine
    from hermes_workflows.observer import associate, native_recorders
    from hermes_workflows.settings import save
    from hermes_workflows.telemetry import TelemetryStore
    from tools.approval_context import _is_interactive_cli
    from tools.approval_prompt import _ask_human

    db = Database(home / "workflows/runs.db")
    save(db, {"trace_enabled": True})
    engine = Engine(db)
    wf = dict(
        id="observer-capture",
        name="Capture",
        version=1,
        scope={"type": "global"},
        trigger={"type": "manual"},
        nodes=[dict(id="work", type="agent_task", prompt="capture", profile="default")],
        edges=[],
    )
    run = engine.start(wf)
    rid = run["run_id"]
    run = db.mutate_run(
        rid, lambda s: (s.update(status="running"), s["nodes"]["work"].update(status="running"))
    )
    att = run["nodes"]["work"]["attempt_id"]
    context = dict(
        db=str(db.path), run_id=rid, node_id="work", attempt_id=att, revision=0, stream_key="direct"
    )
    store = TelemetryStore(db.path)
    api = API(SimpleNamespace(db=db, engine=engine, specs=None, telemetry=store))

    def answer(future, choice="once"):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if future.done():
                raise AssertionError(f"worker exited before approval: {future.result()}")
            approval = store.aggregate(att).get("approval", {})
            if approval.get("can_respond"):
                payload = dict(node_id="work", attempt_id=att, choice=choice)
                assert (
                    api.handle(
                        "POST",
                        f"/runs/{rid}/approvals/{approval['request_id']}",
                        {**payload, "attempt_id": "obsolete"},
                    )[0]
                    == 409
                )
                assert (
                    api.handle("POST", f"/runs/{rid}/approvals/{approval['request_id']}", payload)[
                        0
                    ]
                    == 200
                )
                return
            time.sleep(0.02)
        raise AssertionError("no command approval arrived")

    class CaptureAgent:
        expected_choice = "once"

        def __init__(self, **kwargs):
            pass

        def run_conversation(self, prompt):
            assert _is_interactive_cli()
            from tools.terminal_tool import _get_approval_callback

            plugins.invoke_hook(
                "post_api_request",
                usage={"input_tokens": 7, "output_tokens": 2},
                api_request_id="capture-request",
            )
            plugins.invoke_hook("post_tool_call", status="success", tool_name="capture")
            choice = _ask_human(
                "fixture command", "capture only", 3, False, _get_approval_callback(), False, False
            )
            assert choice == self.expected_choice, choice
            return {"completed": True, "final_response": "CAPTURE_OK"}

        def close(self):
            pass

    with (
        patch("hermes_cli.env_loader.load_hermes_dotenv", lambda **k: None),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **k: {}),
        patch.dict(sys.modules, {"run_agent": SimpleNamespace(AIAgent=CaptureAgent)}),
    ):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(
                run_agent, wf["nodes"][0], telemetry_context=context, worker_approvals_enabled=True
            )
            answer(future)
            assert future.result(10)["output"] == "CAPTURE_OK"
        CaptureAgent.expected_choice = "deny"
        with patch("hermes_workflows.observer.install_direct", return_value=(None, [])):
            assert (
                run_agent(wf["nodes"][0], telemetry_context=context, worker_approvals_enabled=True)[
                    "output"
                ]
                == "CAPTURE_OK"
            )
    observed = store.aggregate(att)
    assert observed["total_tokens"] == 9 and observed["tool_calls"] == 1, observed
    manager = plugins.get_plugin_manager()
    registered = manager.get_approval_transport("workflow-dashboard")
    assert registered is not None
    request = ApprovalRequest.create(
        command="fixture transport",
        description="capture only",
        pattern_key="fixture",
        pattern_keys=("fixture",),
        session_key="isolated",
        surface="cli",
        allow_session=True,
        allow_permanent=True,
        timeout_seconds=5,
    )
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(
            invoke_approval_transport,
            registered.present,
            request,
            timeout_seconds=5,
            poll_interval=0.01,
        )
        answer(future)
        result = future.result(10)
        assert result.choice == "once" and result.failure is None, result

    # Real native schema/dispatcher, with a capture-only spawn seam.
    kb.create_board("observer-qa")
    conn = native_store.connect(board="observer-qa")
    try:
        card = kb.create_task(
            conn,
            title="Capture observer",
            body="No model",
            assignee="default",
            created_by="workflow-qa",
            board="observer-qa",
        )
        associate(conn, db.path, rid, "work", att, 0, card)
        tick = dispatch.dispatch_once(
            conn,
            board="observer-qa",
            spawn_fn=lambda *a, **k: None,
            max_in_progress=1,
            max_in_progress_per_profile=1,
        )
        assert len(tick.spawned) == 1, tick
        task = kb.get_task(conn, card)
        with conn:
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), card))
            kb._append_event(
                conn, card, "worker_bound", {"pid": os.getpid()}, run_id=task.current_run_id
            )
        native_env = dict(
            HERMES_KANBAN_TASK=card,
            HERMES_KANBAN_RUN_ID=str(task.current_run_id),
            HERMES_KANBAN_CLAIM_LOCK=task.claim_lock,
            HERMES_KANBAN_DB=str(Path(kb.kanban_db_path(board="observer-qa")).resolve()),
        )
        with patch.dict(os.environ, native_env):
            observers = native_recorders()
            assert len(observers) == 1, observers
            observers[0].record("api", usage={"input_tokens": 3})
            assert native_recorders() == [], "duplicate imports must not replace a live stream"
            with patch("os.getpid", return_value=os.getpid() + 100):
                assert native_recorders() == [], "delegated children must not register"
            with patch.dict(os.environ, {"HERMES_KANBAN_CLAIM_LOCK": "obsolete"}):
                assert native_recorders() == []
            from tools.approval import check_dangerous_command

            with patch.dict(
                os.environ,
                {
                    "HERMES_INTERACTIVE": "",
                    "HERMES_GATEWAY_SESSION": "",
                    "HERMES_SINGLE_QUERY_SESSION": "1",
                },
            ):
                denied = check_dangerous_command("rm -rf /tmp/capture-only-never-executed", "local")
                assert denied["approved"] is False, denied
            with conn:
                conn.execute("UPDATE tasks SET claim_lock='revoked' WHERE id=?", (card,))
            try:
                store.write(observers[0].identity, {}, 2)
                raise AssertionError("revoked native claim accepted")
            except ConflictError:
                pass
            with conn:
                conn.execute("UPDATE tasks SET claim_lock=? WHERE id=?", (task.claim_lock, card))
            assert kb.complete_task(
                conn,
                card,
                summary="Capture-only completion",
                expected_run_id=task.current_run_id,
                fire_lifecycle_hook=False,
            )
            observers[0].record("tool", tool_call_id="completion", status="success")
            with store.connect() as saved:
                data = json.loads(
                    saved.execute(
                        "SELECT data FROM py_workers WHERE worker_id=?", (observers[0].identity,)
                    ).fetchone()[0]
                )
                assert data["tool_calls"] == 1, data
                requests_before = saved.execute(
                    "SELECT count(*) FROM py_worker_approvals"
                ).fetchone()[0]
            assert observers[0].approval("capture-only", "Finished worker", timeout=0.1) == "deny"
            assert native_recorders() == [], "finished workers cannot register"
            with store.connect() as saved:
                assert (
                    saved.execute("SELECT count(*) FROM py_worker_approvals").fetchone()[0]
                    == requests_before
                )
            # The observation exception cannot outlive archive or a newer run.
            with conn:
                conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (card,))
            try:
                store.write(observers[0].identity, {}, 99)
                raise AssertionError("archived native worker accepted")
            except ConflictError:
                pass
            with conn:
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (card,))
                conn.execute(
                    "INSERT INTO task_runs(task_id,status,started_at,ended_at,outcome) VALUES(?,'done',0,1,'completed')",
                    (card,),
                )
            try:
                store.write(observers[0].identity, {}, 99)
                raise AssertionError("superseded native completion accepted")
            except ConflictError:
                pass
    finally:
        conn.close()
        manager.unload()
    return dict(
        native_worker_hooks="passed",
        direct_callback="passed",
        native_bound_transport="passed",
        board_claim_and_child_fences="passed",
        native_headless_policy="passed",
        final_completion_observation="passed",
        provider_requests=0,
        executed_commands=0,
        external_messages=0,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    activate(args.hermes_source)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
    with tempfile.TemporaryDirectory(prefix="workflow native observer żółć ") as temporary:
        home = Path(temporary).resolve()
        for key in list(os.environ):
            if key.startswith("HERMES_KANBAN_"):
                os.environ.pop(key)
        os.environ.update(
            HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), HERMES_WORKFLOWS_WORKER="1"
        )
        (home / "config.yaml").write_text(
            "model: capture-only\nplugins:\n  isolation: in_process\n", encoding="utf-8"
        )
        print(json.dumps(verify(home), indent=2))


if __name__ == "__main__":
    main()
