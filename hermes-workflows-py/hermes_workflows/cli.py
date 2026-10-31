"""Portable CLI: python -m hermes_workflows --help."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from .api import API
from .runtime import get_runtime
from .specs import parse_document, validate


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hermes-workflows-py")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="keep the API, scheduler and engine running")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9123)
    serve.add_argument("--token")
    commands.add_parser("list")
    load = commands.add_parser("import", help="import a JSON or YAML workflow")
    load.add_argument("path", type=Path)
    validate_command = commands.add_parser("validate")
    validate_command.add_argument("path", type=Path)
    run = commands.add_parser("run", help="run until completion or a human review gate")
    run.add_argument("workflow_id")
    run.add_argument("--project-id")
    run.add_argument("--input")
    run.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    for name in ("status", "cancel", "retry", "export"):
        p = commands.add_parser(name)
        p.add_argument("run_id")
        if name == "retry":
            p.add_argument("--node-id")
    review = commands.add_parser("review")
    review.add_argument("run_id")
    review.add_argument("node_id")
    review.add_argument("decision", choices=("approved", "rejected", "needs_changes"))
    review.add_argument("--note")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    runtime = None
    try:
        if args.command == "validate":
            data = parse_document(args.path.read_text(encoding="utf-8-sig"))
            result = validate(data.get("workflow", data))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        runtime = get_runtime()
        api = API(runtime)
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
            params = {}
            for value in args.param:
                if "=" not in value:
                    raise ValueError("--param must be NAME=VALUE")
                name, value = value.split("=", 1)
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
                {"node_id": args.node_id, "decision": args.decision, "note": args.note},
            )
        elif args.command == "retry":
            status, result = api.handle(
                "POST",
                f"/runs/{args.run_id}/retry",
                {"node_id": args.node_id} if args.node_id else {},
            )
        else:
            status, result = api.handle("POST", f"/runs/{args.run_id}/cancel")
        if status < 400 and args.command in ("run", "review", "retry"):
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
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        if runtime:
            runtime.close()
