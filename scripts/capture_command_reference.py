"""Freeze pure pinned command/resume decisions without importing Hermes or Bun."""

import argparse
import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Sequence

PIN = "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=Path(".upstream"))
    parser.add_argument(
        "--output", type=Path, default=Path("tests/fixtures/command-reference.json")
    )
    args = parser.parse_args()
    hashes = {}

    def read(path):
        raw = subprocess.check_output(["git", "-C", str(args.upstream), "show", f"{PIN}:{path}"])
        hashes[path] = hashlib.sha256(raw).hexdigest()
        return ast.parse(raw)

    def functions(tree, names):
        found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
        assert {n.name for n in found} == set(names)
        return ast.Module(body=found, type_ignores=[])

    ns = {
        "Optional": Optional,
        "Sequence": Sequence,
        "NodeSignature": tuple[str, str],
        "Any": Any,
        "Callable": Callable,
        "Mapping": Mapping,
        "argparse": argparse,
    }
    helpers = read("hermes_workflows/resume.py")
    exec(
        compile(
            functions(helpers, [n.name for n in helpers.body if isinstance(n, ast.FunctionDef)]),
            "resume.py",
            "exec",
        ),
        ns,
    )
    engine = read("hermes_workflows/engine.py")
    ns.update(
        resume_mod=SimpleNamespace(structural_drift=ns["structural_drift"]),
        ACTIVE_RUN_STATUSES={"created", "running", "waiting"},
        ResumeError=type("ResumeError", (ValueError,), {}),
    )
    exec(compile(functions(engine, ["resume_reset", "_inline_permitted"]), "engine.py", "exec"), ns)
    exec(
        compile(
            functions(read("hermes_workflows/tools.py"), ["resolve_nl_command"]), "tools.py", "exec"
        ),
        ns,
    )
    exec(
        compile(
            functions(read("hermes_workflows/plugin.py"), ["_parse_run_args"]), "plugin.py", "exec"
        ),
        ns,
    )
    exec(compile(functions(read("hermes_workflows/wait.py"), ["evaluate"]), "wait.py", "exec"), ns)
    exec(compile(functions(read("hermes_workflows/cli.py"), ["_parser"]), "cli.py", "exec"), ns)
    wf = {
        "id": "reference",
        "name": "Reference",
        "version": 1,
        "scope": {"type": "global"},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "qa"},
        "nodes": [
            {"id": "a", "type": "agent_task", "prompt": "prefix"},
            {"id": "b", "type": "agent_task", "prompt": "fix me"},
            {"id": "end", "type": "finish"},
        ],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "end"}],
    }
    run = {
        "run_id": "reference-run",
        "workflow_id": "reference",
        "status": "failed",
        "nodes": {
            n["id"]: {
                "node_type": n["type"],
                "status": "completed"
                if n["id"] == "a"
                else "failed"
                if n["id"] == "b"
                else "pending",
            }
            for n in wf["nodes"]
        },
    }
    cases = []
    for name in (
        "single",
        "none",
        "many",
        "created",
        "running",
        "waiting",
        "explicit",
        "explicit_success",
        "unknown",
        "all",
        "completed_all",
        "legacy",
        "retyped",
        "added",
        "removed",
        "content_edit",
        "edge_edit",
    ):
        state, live = copy.deepcopy(run), copy.deepcopy(wf)
        node, all_nodes = None, False
        if name in ("none", "explicit_success"):
            state["nodes"]["b"]["status"] = "completed"
        if name == "many":
            state["nodes"]["a"]["status"] = "failed"
        if name in ("created", "running", "waiting"):
            state["status"] = name
        if name in ("explicit", "explicit_success"):
            node = "b"
        if name == "unknown":
            node = "absent"
        if name in ("all", "completed_all"):
            all_nodes = True
        if name == "completed_all":
            state["status"] = "completed"
        if name == "legacy":
            for value in state["nodes"].values():
                value.pop("node_type")
        if name == "retyped":
            live["nodes"][1] = {"id": "b", "type": "script", "command": "echo ok"}
        if name == "added":
            live["nodes"].append({"id": "extra", "type": "condition"})
        if name == "removed":
            live["nodes"].pop()
        if name == "content_edit":
            live["nodes"][1]["prompt"] = "fixed"
        if name == "edge_edit":
            live["edges"] = [{"from": "a", "to": "b"}, {"from": "a", "to": "end"}]
        record = {"name": name, "run": state, "workflow": live, "node": node, "all": all_nodes}

        def core(argv, live=live, state=state):
            if argv[0] == "spec-get":
                return {"workflow": live}
            target = argv[argv.index("--node") + 1] if "--node" in argv else None
            if target is not None and (
                target not in state["nodes"] or state["nodes"][target]["status"] != "failed"
            ):
                raise ValueError("target must be failed")
            return {"target": target, "all": target is None}

        stub = SimpleNamespace(
            db_path="reference.db",
            _load=lambda _, state=state: state,
            _spec_path_for_run=lambda *_: "reference.workflow.json",
            _core=core,
        )
        try:
            record["expected"] = ns["resume_reset"](
                stub, [], state["run_id"], node=node, reset_all=all_nodes
            )[0]
        except ValueError:
            record["expected"] = {"refused": True}
        cases.append(record)
    catalog = [
        {"id": "release", "name": "Feature Release"},
        {"id": "release-docs", "name": "Feature Release Docs"},
        {"id": "audit", "name": "Audit"},
    ]
    nl = [
        {"text": text, "expected": ns["resolve_nl_command"](text, catalog)}
        for text in (
            "",
            "unknown",
            "release",
            "RELEASE scope 3",
            "Feature Release ship it",
            "Feature Release Docs only docs",
            "audit żółć",
        )
    ]
    ambiguous = [{"id": "one", "name": "Same"}, {"id": "two", "name": "Same"}]
    result = {
        "reference": {"commit": PIN, "source_sha256": hashes},
        "resumes": cases,
        "catalog": catalog,
        "natural_language": nl,
        "ambiguous": {
            "catalog": ambiguous,
            "text": "Same ship",
            "expected": ns["resolve_nl_command"]("Same ship", ambiguous),
        },
        "inline_modes": {
            mode: ns["_inline_permitted"](SimpleNamespace(default_mode=mode))
            for mode in ("durable", "direct", "auto")
        },
        "run_arguments": [],
        "cli_arguments": [],
        "pr_states": [
            {
                "state": state,
                "expected": ns["evaluate"](
                    {"github_pr_merged": "12"}, gh_state=lambda _, state=state: state
                ),
            }
            for state in ("MERGED", "CLOSED", "OPEN", None, "UNKNOWN")
        ],
        "python_decisions": {
            "failure_state": "completed/outcome=failure for executable nodes is a failed target",
            "descendants": "reopen skipped/cancelled descendants on resume; keep settled prefix and attempt history",
            "slash_response": "retain existing JSON responses and Windows literal backslashes",
            "mode": "durable releases the lease after a settled batch; direct drains at most 64 script-only batches per lease; both use resident async supervision",
        },
    }
    for tokens in (
        [],
        ["demo", "count=3", "text=two words"],
        ["count=3"],
        ["=bad"],
        ["demo", "stray"],
    ):
        try:
            project, params = ns["_parse_run_args"](tokens)
            expected = {"project_id": project, "params": params}
        except ValueError:
            expected = {"refused": True}
        result["run_arguments"].append({"tokens": tokens, "expected": expected})
    for tokens in (
        ["resume", "r"],
        ["resume", "r", "--node", "b"],
        ["resume", "r", "--all"],
        [
            "run",
            "qa",
            "--project",
            "demo",
            "--origin",
            "discord:fixture",
            "--input",
            "scope 3",
            "--params",
            '{"count":3}',
        ],
    ):
        result["cli_arguments"].append(
            {"tokens": tokens, "expected": vars(ns["_parser"]().parse_args(tokens))}
        )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
