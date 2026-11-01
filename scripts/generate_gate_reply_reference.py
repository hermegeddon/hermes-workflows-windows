"""Freeze reply interpretations by executing only the pinned pure functions."""

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Optional, Tuple


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=Path(".upstream"))
    parser.add_argument(
        "--output", type=Path, default=Path("tests/fixtures/gate-reply-reference.json")
    )
    args = parser.parse_args()
    pin = "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f"
    source = "hermes_workflows/gate_reply.py"
    raw = subprocess.check_output(["git", "-C", str(args.upstream), "show", f"{pin}:{source}"])
    wanted = {"_decision_and_note", "_interpret_reply"}
    functions = [
        n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef) and n.name in wanted
    ]
    assert {n.name for n in functions} == wanted
    namespace = {
        "Optional": Optional,
        "Tuple": Tuple,
        "REVIEW_OPTIONS": ("approved", "rejected", "needs_changes"),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), source, "exec"), namespace)
    texts = [
        "",
        "  ",
        "approved",
        "Approved ship it",
        "Rejected fix this",
        "NEEDS_CHANGES",
        " needs_changes  fix lints ",
        "3",
        "scope 3",
        "Native Obsidian views",
        "żółć\nsecond line",
        "yes please",
        "approved, ship it",
    ]
    data = {
        "reference": {"commit": pin, "source_sha256": {source: hashlib.sha256(raw).hexdigest()}},
        "interpretations": [
            {"text": text, "expected": namespace["_interpret_reply"](text)} for text in texts
        ],
    }
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
