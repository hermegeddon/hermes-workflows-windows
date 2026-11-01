"""Lazy native worker hooks. No supervisor or provider is started by observers."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .telemetry import NodeTelemetryRecorder, TelemetryStore

HOOKS = {
    "post_api_request": "api",
    "api_request_error": "api_error",
    "post_tool_call": "tool",
    "subagent_stop": "subagent",
    "pre_approval_request": "approval_request",
    "post_approval_response": "approval_response",
}


def associate(conn, db, run_id, node_id, attempt_id, revision, task_id):
    """Publish optional correlation in the native transaction BEFORE dispatch."""
    conn.execute("SAVEPOINT optional_observer")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hermes_workflows_observers (task_id TEXT NOT NULL, attempt_id TEXT NOT NULL, context TEXT NOT NULL, PRIMARY KEY(task_id,attempt_id))"
        )
        context = {
            "db": str(Path(db).resolve()),
            "run_id": run_id,
            "node_id": node_id,
            "attempt_id": attempt_id,
            "revision": revision,
            "stream_key": task_id,
        }
        conn.execute(
            "INSERT OR IGNORE INTO hermes_workflows_observers VALUES(?,?,?)",
            (task_id, attempt_id, json.dumps(context)),
        )
    except Exception:
        conn.execute("ROLLBACK TO optional_observer")
    finally:
        conn.execute("RELEASE optional_observer")


def native_recorders():
    task_id, run_id = os.environ.get("HERMES_KANBAN_TASK"), os.environ.get("HERMES_KANBAN_RUN_ID")
    raw = os.environ.get("HERMES_KANBAN_DB")
    if not task_id or not run_id or not raw:
        return []
    try:
        from hermes_cli.profiles import resolve_profile_env
        from hermes_constants import get_hermes_home

        with closing(
            sqlite3.connect(Path(raw).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        ) as con:
            con.row_factory = sqlite3.Row
            task = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if (
                not task
                or task["status"] != "running"
                or task["current_run_id"] != int(run_id)
                or task["claim_lock"] != os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
                or Path(resolve_profile_env(task["assignee"])).resolve()
                != Path(get_hermes_home()).resolve()
            ):
                return []
            bound = con.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_bound' ORDER BY id DESC LIMIT 1",
                (task_id, int(run_id)),
            ).fetchone()
            if (json.loads(bound[0]).get("pid") if bound else task["worker_pid"]) != os.getpid():
                return []  # A delegated child cannot replace its parent's stream.
            rows = con.execute(
                "SELECT context FROM hermes_workflows_observers WHERE task_id=? ORDER BY rowid DESC LIMIT 16",
                (task_id,),
            ).fetchall()
        recorders = []
        for row in rows:
            try:
                context = json.loads(row[0])
                context.update(
                    native_run_id=int(run_id),
                    profile_home=str(Path(get_hermes_home()).resolve()),
                    native_db=str(Path(raw).resolve()),
                    native_claim=task["claim_lock"],
                    native_pid=os.getpid(),
                )
                recorders.append(NodeTelemetryRecorder(TelemetryStore(context["db"]), context))
            except Exception:
                continue
        return recorders
    except Exception:
        return []


def install(ctx, recorders):
    if not recorders:
        return []
    handles = []
    try:
        return _install(ctx, recorders, handles)
    except Exception:
        for handle in handles:
            try:
                handle.dispose()
            except Exception:
                pass
        raise


def _install(ctx, recorders, handles):
    for name, kind in HOOKS.items():

        def callback(_kind=kind, **fields):
            for recorder in recorders:
                recorder.record(_kind, **fields)

        handles.append(ctx.register_hook(name, callback))
    transport = getattr(ctx, "register_approval_transport", None)
    if callable(transport):

        def present(request):
            # Native policy chooses this transport explicitly. Multiple workflow
            # observers on an adopted card cannot guess which operator owns it.
            choice = (
                recorders[0].approval(
                    request.command,
                    request.description,
                    timeout=request.timeout_seconds,
                    native_request=request,
                )
                if len(recorders) == 1
                else "deny"
            )
            return request.respond(choice)

        transport("workflow-dashboard", present)
    return handles


def register_worker_observers(ctx):
    try:
        install(ctx, native_recorders())
    except Exception:
        pass


def install_direct(context):
    """The direct worker has this package even if its selected profile does not."""
    try:
        from hermes_cli.plugins import PluginContext, get_plugin_manager
        from hermes_cli.plugins_manifest import parse_manifest_file

        recorder = NodeTelemetryRecorder(TelemetryStore(context["db"]), context)
        root = Path(__file__).resolve().parent.parent
        manifest = parse_manifest_file(root / "plugin.yaml", root, "user", "")
        handles = install(PluginContext(manifest, get_plugin_manager()), [recorder])
        return recorder, handles
    except Exception:
        return None, []
