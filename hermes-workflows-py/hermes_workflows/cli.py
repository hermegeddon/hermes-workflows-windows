"""Portable CLI: python -m hermes_workflows --help."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from .api import API
from .runtime import Runtime, get_runtime
from .specs import parse_document, validate


async def _advance(runtime, run_ids, *, all_runs=False):
    async def drive(run_id):
        runtime.db.get_run(run_id)
        owner = "cli:" + uuid.uuid4().hex
        if runtime.db.claim(run_id, owner):
            await runtime.engine.drive(run_id, owner)
        return runtime.db.get_run(run_id)

    if not all_runs:
        try:
            return await drive(run_ids[0])
        except KeyError as exc:
            raise ValueError("unknown run") from exc
    results = await asyncio.gather(
        *(drive(identity) for identity in run_ids), return_exceptions=True
    )
    advanced = []
    for identity, result in zip(run_ids, results, strict=True):
        if isinstance(result, BaseException):
            logging.warning("could not advance run %s: %s", identity, result)
        else:
            advanced.append(result)
    return {
        "advanced": advanced,
        "dispatched": [],
        "active": any(run["status"] in ("created", "running", "waiting") for run in advanced),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hermes-workflows-py")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="keep the API, scheduler and engine running")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9123)
    serve.add_argument("--token")
    commands.add_parser("list")
    commands.add_parser("advance-all", help="drive existing active runs; preserve parked reviews")
    advance = commands.add_parser(
        "advance-run", help="drive one existing run without starting siblings"
    )
    advance.add_argument("run_id")
    explain = commands.add_parser("explain")
    explain.add_argument("workflow_id")
    load = commands.add_parser("import", help="import a JSON or YAML workflow")
    load.add_argument("path", type=Path)
    validate_command = commands.add_parser("validate")
    validate_command.add_argument("path", type=Path)
    run = commands.add_parser("run", help="run until completion or a human review gate")
    run.add_argument("workflow_id")
    run.add_argument("--project-id", "--project", dest="project_id")
    run.add_argument("--origin")
    run.add_argument("--input")
    run.add_argument("--params", help="template parameter values as a JSON object")
    run.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    for name in ("status", "cancel", "retry", "export"):
        p = commands.add_parser(name)
        p.add_argument("run_id")
        if name == "retry":
            p.add_argument("--node-id")
        if name == "export":
            p.add_argument("--as-template", action="store_true", help="export a workflow template instead of run logs")
            p.add_argument("--out-dir", type=Path)
            p.add_argument("--with-hints", action="store_true", help="opt in to one bounded model call for adaptation advice")
            p.add_argument("--model", help="model[@provider] for optional advice")
            p.add_argument("--hint-timeout", type=float, default=120)
    review = commands.add_parser("review")
    review.add_argument("run_id")
    review.add_argument("node_id")
    review.add_argument("decision", choices=("approved", "rejected", "needs_changes"))
    review.add_argument("--note")
    review.add_argument("--attempt-id", help="decide only the displayed review attempt")
    resume = commands.add_parser("resume", help="resume the single failed node under the live spec")
    resume.add_argument("run_id")
    target = resume.add_mutually_exclusive_group()
    target.add_argument("--node", "--node-id", dest="node")
    target.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "export" and not args.as_template and (
        args.out_dir or args.with_hints or args.model or args.hint_timeout != 120
    ):
        parser.error("template options require --as-template")
    logging.basicConfig(level=logging.INFO)
    runtime = None
    try:
        if args.command == "validate":
            data = parse_document(args.path.read_text(encoding="utf-8-sig"))
            result = validate(data.get("workflow", data))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        if args.command in ("advance-all", "advance-run"):
            # Compatibility entry points do not start a profile-wide scheduler.
            # Keep this process alive for work it owns; another owner is untouched.
            runtime = Runtime()
            identities = runtime.db.active_ids() if args.command == "advance-all" else [args.run_id]
            result = asyncio.run(
                _advance(runtime, identities, all_runs=args.command == "advance-all")
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result.get("status") == "failed" else 0
        if args.command == "export" and args.as_template:
            from .template_export import export, write_bundle

            runtime = Runtime()  # Export never starts workers or the scheduler.
            workflow = runtime.specs.get(args.run_id)["workflow"]
            result = export(workflow, runtime.db, with_hints=args.with_hints, model=args.model,
                            hint_timeout=args.hint_timeout)
            directory = args.out_dir or runtime.home / "workflows" / "template-exports"
            print(json.dumps(write_bundle(result, directory), ensure_ascii=False, indent=2))
            return 0
        runtime = get_runtime()
        api = API(runtime, origin=getattr(args, "origin", None))
        if args.command == "serve":
            from .server import make_server

            server = make_server(args.host, args.port, api=api, token=args.token)
            print(
                f"Workflow API listening on http://{args.host}:{server.server_port}/api/plugins/hermes-workflows",
                flush=True,
            )
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        if args.command == "list":
            status, result = api.handle("GET", "/workflows")
        elif args.command == "explain":
            status, result = api.handle("POST", f"/workflows/{args.workflow_id}/compile-preview")
        elif args.command == "import":
            document = parse_document(args.path.read_text(encoding="utf-8-sig"))
            payload = (
                document
                if "workflow" in document
                else {
                    "workflow": {k: v for k, v in document.items() if k != "ui"},
                    **({"ui": document["ui"]} if "ui" in document else {}),
                }
            )
            status, result = api.handle("POST", "/workflows", payload)
        elif args.command == "run":
            params = json.loads(args.params) if args.params is not None else {}
            if not isinstance(params, dict):
                raise ValueError("--params must be a JSON object")
            for value in args.param:
                if "=" not in value:
                    raise ValueError("--param must be NAME=VALUE")
                name, value = value.split("=", 1)
                if not name:
                    raise ValueError("--param must be NAME=VALUE")
                params[name] = value
            status, result = api.handle(
                "POST",
                f"/workflows/{args.workflow_id}/run",
                {"project_id": args.project_id, "input": args.input, "params": params},
            )
        elif args.command == "status":
            status, result = api.handle("GET", f"/runs/{args.run_id}")
        elif args.command == "export":
            status, result = api.handle("GET", f"/runs/{args.run_id}/export")
        elif args.command == "review":
            status, result = api.handle(
                "POST",
                f"/runs/{args.run_id}/review",
                {
                    "node_id": args.node_id,
                    "decision": args.decision,
                    "note": args.note,
                    "attempt_id": args.attempt_id,
                },
            )
        elif args.command == "retry":
            status, result = api.handle(
                "POST",
                f"/runs/{args.run_id}/retry",
                {"node_id": args.node_id} if args.node_id else {},
            )
        elif args.command == "resume":
            status, result = api.handle(
                "POST",
                f"/runs/{args.run_id}/resume",
                {"node": args.node, "all": args.all},
            )
        else:
            status, result = api.handle("POST", f"/runs/{args.run_id}/cancel")
        if status < 400 and args.command in ("run", "review", "retry", "resume"):
            run_id = result["run_id"]
            while True:
                result = runtime.db.get_run(run_id)
                if result["status"] in ("waiting", "completed", "failed", "cancelled"):
                    break
                time.sleep(0.1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if status >= 400 or result.get("status") == "failed" else 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        if runtime:
            runtime.close()
