"""Freeze isolated pinned Python observer/trace decisions, without its Bun host."""

import ast
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".upstream/hermes_workflows"


def definition(file, name, parent=None):
    tree = ast.parse((SOURCE / file).read_text(encoding="utf-8"))
    nodes = tree.body
    if parent:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == parent).body
    node = next(n for n in nodes if getattr(n, "name", None) == name)
    namespace = dict(Path=Path, time=time, Any=Any, Callable=Callable, Optional=Optional)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE / file), "exec"), namespace)
    return namespace[name]


def capture():
    cls = definition("telemetry.py", "NodeTelemetryRecorder")
    cls._flush = lambda self: None
    events = [
        ["api", 10, {"usage": {"prompt_tokens": 7, "completion_tokens": 2}}],
        ["api", 11, {"usage": {"input_tokens": 3, "output_tokens": 1}}],
        ["api_error", 12, {"error": {"type": "Timeout", "message": "retry later"}}],
        ["tool", 13, {"status": "success"}],
        ["tool", 14, {"status": "error", "error_type": "Missing", "error_message": "gone"}],
        ["subagent", 15, {}],
        [
            "approval_request",
            20,
            {"command": "fixture command", "description": "confirm", "surface": "cli"},
        ],
        ["approval_response", 25, {"choice": "deny"}],
    ]
    clock = [0]
    recorder = cls(Path("unused"), "fixture", now=lambda: clock[0])
    names = {
        "api": "record_api_request",
        "api_error": "record_api_error",
        "tool": "record_tool_call",
        "subagent": "record_subagent",
        "approval_request": "record_approval_request",
        "approval_response": "record_approval_response",
    }
    snapshots = []
    for kind, ts, fields in events:
        clock[0] = ts
        getattr(recorder, names[kind])(**fields)
        snapshots.append(json.loads(json.dumps(recorder._data)))
    trace = []
    emitter = SimpleNamespace(
        _trace_emit=lambda run_id, kind, **fields: trace.append(dict(kind=kind, **fields))
    )
    before = dict(statuses={"work": "scheduled"}, run_status="running", notified=[])
    after = dict(
        run_id="fixture",
        status="completed",
        nodes={"work": dict(status="completed", outcome="success", seq=1)},
        notified=["completed"],
    )
    definition("engine.py", "_emit_trace", "Engine")(emitter, before, after)
    result = {
        "upstream_commit": "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f",
        "source_sha256": {
            file: hashlib.sha256((SOURCE / file).read_bytes()).hexdigest()
            for file in ("telemetry.py", "engine.py")
        },
        "events": events,
        "snapshots": snapshots,
        "trace": trace,
    }
    target = ROOT / "tests/fixtures/telemetry-reference.json"
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    capture()
