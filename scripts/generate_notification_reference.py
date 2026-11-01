"""Freeze target, SILENT and final-output decisions from the pinned Python engine."""

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Optional


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=Path(".upstream"))
    parser.add_argument(
        "--output", type=Path, default=Path("tests/fixtures/notification-reference.json")
    )
    args = parser.parse_args()
    pin = "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f"
    namespace = {"Optional": Optional, "SILENT_MARKER": "[SILENT]"}
    hashes = {}
    for source, wanted in [
        ("hermes_workflows/notifications.py", {"resolve_target", "is_silenced"}),
        ("hermes_workflows/engine.py", {"_run_result_output"}),
    ]:
        raw = subprocess.check_output(["git", "-C", str(args.upstream), "show", f"{pin}:{source}"])
        hashes[source] = hashlib.sha256(raw).hexdigest()
        functions = [
            n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef) and n.name in wanted
        ]
        assert {n.name for n in functions} == wanted
        exec(compile(ast.Module(body=functions, type_ignores=[]), source, "exec"), namespace)
    targets = [
        (None, None, None),
        ("telegram:one", "local", None),
        (None, "discord:default", None),
        ("telegram:one", "local", "origin"),
        (None, "local", "origin"),
        (None, None, "origin"),
        ("telegram:one", "local", "discord:explicit"),
        (None, None, "local"),
    ]
    texts = [None, "", "result [SILENT] trailing", "[silent] ordinary output", "żółć"]
    outputs = [
        {},
        {
            "work": {"status": "completed", "output": "first", "seq": 1},
            "finish": {"status": "completed", "seq": 3},
        },
        {
            "failed": {"status": "failed", "output": "ignore", "seq": 9},
            "work": {"status": "completed", "output": "keep", "seq": 2},
        },
        {
            "a": {"status": "completed", "output": "a", "seq": 1},
            "b": {"status": "completed", "output": "b", "seq": 1},
        },
        {
            "a": {"status": "completed", "output": "old", "seq": 1},
            "b": {"status": "completed", "output": "latest", "seq": 8},
        },
    ]
    data = {
        "reference": {"commit": pin, "source_sha256": hashes},
        "targets": [
            {
                "origin": o,
                "default": d,
                "deliver": t,
                "expected": namespace["resolve_target"](o, d, t),
            }
            for o, d, t in targets
        ],
        "silent": [{"text": t, "expected": namespace["is_silenced"](t)} for t in texts],
        "outputs": [
            {"nodes": n, "expected": namespace["_run_result_output"]({"nodes": n})} for n in outputs
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
