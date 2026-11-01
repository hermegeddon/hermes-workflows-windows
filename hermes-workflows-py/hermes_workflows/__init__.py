"""Hermes registration. Importing the plugin has no runtime side effects."""

from __future__ import annotations

import json
import os
from functools import partial

__version__ = "1.0.2"


def _object(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _string():
    return {"type": "string"}


TOOL_SCHEMAS = {
    "workflow_list": _object({}),
    "workflow_run": _object(
        {
            "workflow_id": _string(),
            "project_id": _string(),
            "input": _string(),
            "params": {"type": "object"},
        },
        ("workflow_id",),
    ),
    "workflow_status": _object({"run_id": _string()}, ("run_id",)),
    "workflow_explain": _object({"workflow_id": _string()}, ("workflow_id",)),
    "workflow_review": _object(
        {
            "run_id": _string(),
            "node_id": _string(),
            "decision": {"type": "string", "enum": ["approved", "rejected", "needs_changes"]},
            "note": _string(),
            "attempt_id": {
                "type": "string",
                "description": "The displayed review attempt_id from workflow_status; rejects stale decisions.",
            },
        },
        ("run_id", "node_id", "decision"),
    ),
    "workflow_resume": _object(
        {"run_id": _string(), "node": _string(), "all": {"type": "boolean"}},
        ("run_id",),
    ),
}


def _tool(name, args, home=None, *, task_id=None):
    from .api import API, BASE
    from .runtime import get_runtime

    args = {} if args is None else args
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be an object")
    runtime = get_runtime(home)
    api = API(
        runtime,
        origin=runtime.origins.origin_for(task_id),
        origin_metadata=runtime.origins.routing_for(task_id),
    )
    if name == "workflow_list":
        return api.handle("GET", BASE + "/workflows")
    if name == "workflow_run":
        return api.handle(
            "POST",
            BASE + f"/workflows/{args['workflow_id']}/run",
            {k: v for k, v in args.items() if k != "workflow_id"},
        )
    if name == "workflow_status":
        return api.handle("GET", BASE + f"/runs/{args['run_id']}")
    if name == "workflow_explain":
        return api.handle("POST", BASE + f"/workflows/{args['workflow_id']}/compile-preview")
    if name == "workflow_review":
        return api.handle(
            "POST",
            BASE + f"/runs/{args['run_id']}/review",
            {k: v for k, v in args.items() if k != "run_id"},
        )
    if name == "workflow_resume":
        return api.handle(
            "POST", BASE + f"/runs/{args['run_id']}/resume",
            {k: v for k, v in args.items() if k != "run_id"},
        )
    raise ValueError("unknown workflow tool")


def _handler(name, home=None):
    def handle(args=None, **kwargs):
        try:
            status, result = _tool(name, args, home, task_id=kwargs.get("task_id"))
            if status >= 400:
                result = {"error": result["detail"], "status_code": status}
            return json.dumps(result, ensure_ascii=False)
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return json.dumps({"error": str(exc)})

    return handle


def _command(raw_args="", *, home=None, **_kwargs):
    from .api import API
    from .commands import parse_resume_args, parse_run_args, resolve_nl_command, tokenize
    from .runtime import get_runtime

    usage = "Usage: /workflow list | run <id> [project] [name=value ...] [--input text] | status <run> | review <run> <node> <approved|rejected|needs_changes> [note] | resume <run> [--node <id>|--all] | retry <run> [--node-id <id>] | cancel <run> | explain <id>"
    try:
        parts = tokenize(raw_args)
        if not parts or parts[0] in ("help", "-h", "--help"):
            return usage
        sub, tail = parts[0], parts[1:]
        if sub not in ("list", "run", "explain", "status", "review", "cancel", "resume", "retry"):
            status, catalog = _tool("workflow_list", {}, home)
            if status >= 400:
                return json.dumps(catalog, ensure_ascii=False)
            resolved = resolve_nl_command(raw_args, catalog["workflows"])
            if "question" in resolved:
                return resolved["question"]
            _, result = _tool(
                "workflow_run", resolved, home,
                task_id=_kwargs.get("task_id") or _kwargs.get("session_key"),
            )
            return json.dumps(result, ensure_ascii=False)
        args = {}
        if sub in ("run", "explain"):
            args["workflow_id"] = tail.pop(0)
        elif sub in ("status", "review", "cancel", "resume", "retry"):
            args["run_id"] = tail.pop(0)
        if sub == "run":
            if "--input" in tail:
                index = tail.index("--input")
                args["input"] = " ".join(tail[index + 1 :])
                tail = tail[:index]
            project, params = parse_run_args(tail)
            if project is not None:
                args["project_id"] = project
            if params:
                args["params"] = params
        if sub in ("resume", "retry"):
            node, reset_all = parse_resume_args(tail)
            if sub == "resume":
                args.update(all=reset_all)
                if node is not None:
                    args["node"] = node
            else:
                payload = {"node_id": node} if node is not None else {}
                _, result = API(get_runtime(home)).handle("POST", f"/runs/{args['run_id']}/retry", payload)
                return json.dumps(result, ensure_ascii=False)
        if sub in ("list", "status", "cancel", "explain") and tail:
            raise ValueError("unexpected argument")
        if sub == "review":
            args.update(node_id=tail[0], decision=tail[1])
            if len(tail) > 2:
                args["note"] = " ".join(tail[2:])
        if sub == "cancel":
            status, result = API(get_runtime(home)).handle("POST", f"/runs/{args['run_id']}/cancel")
        elif sub in ("list", "run", "status", "review", "explain", "resume"):
            status, result = _tool(
                "workflow_" + sub,
                args,
                home,
                task_id=_kwargs.get("task_id") or _kwargs.get("session_key"),
            )
        else:
            return usage
        return json.dumps(result, ensure_ascii=False)
    except (IndexError, ValueError, KeyError, RuntimeError, OSError) as exc:
        return f"workflow command failed: {exc}\n{usage}"


def register(ctx):
    """Register compatible workflow tools plus explicit resume control.

    Dashboard routes use dashboard/plugin_api.py, the standard Hermes plugin
    handler convention. Hosts that disable third-party router loading can proxy
    the bundled standard-library API server under the same API prefix.
    """
    from .db import hermes_home
    from .runtime import get_runtime, own_runtime

    home = hermes_home()
    # Agent workers load Hermes plugins too. They must not recursively start
    # another workflow supervisor while performing one node of a parent run.
    if os.environ.get("HERMES_KANBAN_TASK") and os.environ.get("HERMES_WORKFLOWS_WORKER") != "1":
        from .observer import register_worker_observers

        register_worker_observers(ctx)
    if os.environ.get("HERMES_WORKFLOWS_WORKER") != "1" and not os.environ.get(
        "HERMES_KANBAN_TASK"
    ):
        runtime = get_runtime(home)
        register_hook = getattr(ctx, "register_hook", None)
        if callable(register_hook):
            register_hook("pre_gateway_dispatch", runtime.pre_dispatch)
        on_unload = getattr(ctx, "on_unload", None)
        if callable(on_unload):
            on_unload(own_runtime(runtime))

    descriptions = {
        "workflow_list": "List available workflows.",
        "workflow_run": "Start a workflow by id; poll workflow_status for progress.",
        "workflow_status": "Get full workflow run state, including per-node outcomes.",
        "workflow_explain": "Preview a workflow's steps without running it.",
        "workflow_review": "Resolve a waiting human review gate and advance the run. Pass its attempt_id from workflow_status to fence the decision to that iteration.",
        "workflow_resume": "Resume the single failed node under the live workflow, preserving completed work. Choose node explicitly when failures are ambiguous, or all for a full restart.",
    }
    for name, schema in TOOL_SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="workflows",
            schema=schema,
            handler=_handler(name, home),
            description=descriptions[name],
        )
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command(
            "workflow",
            partial(_command, home=home),
            description="Run and manage workflows.",
            args_hint="list | run <id> | status <run> | review <run> <node> <decision> | resume <run> [--node <id>|--all] | cancel <run>",
        )
