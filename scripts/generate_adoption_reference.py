"""Freeze pure pinned-upstream adoption decisions without importing its engine."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=Path(".upstream"))
    parser.add_argument(
        "--output", type=Path, default=Path("tests/fixtures/adoption-reference.json")
    )
    args = parser.parse_args()
    source = args.upstream / "hermes_workflows/engine.py"
    pin = "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f"
    commit = subprocess.check_output(
        ["git", "-C", str(args.upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != pin:
        raise RuntimeError("Upstream checkout differs from the reference pin")
    subprocess.run(
        [
            "git",
            "-C",
            str(args.upstream),
            "diff",
            "--exit-code",
            pin,
            "--",
            "hermes_workflows/engine.py",
        ],
        check=True,
    )
    raw = subprocess.check_output(
        ["git", "-C", str(args.upstream), "show", f"{pin}:hermes_workflows/engine.py"]
    )
    tree = ast.parse(raw)
    wanted = {"_TASK_ID_TOKEN", "_TASK_IDS_BLOCK"}
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in wanted for target in node.targets)
        or isinstance(node, ast.FunctionDef)
        and node.name in ("_extract_task_ids_block", "_topological_order")
    ]
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {"re": re, "Optional": __import__("typing").Optional}
    exec(compile(module, str(source), "exec"), namespace)
    extraction = [
        None,
        "just prose t_stray",
        "Locked.\n```task_ids\nt_aaaa\nt_bbbb\nt_aaaa\n```\nignore t_cccc",
        "<task_ids>t_aaaa, t_bbbb</task_ids>",
        "```TASK_IDS\nt_a\n``` <task_ids>t_b t_a</task_ids>",
    ]
    orders = [
        (["c", "a", "b"], [("a", "c"), ("b", "c")]),
        (["c", "a", "b"], [("a", "b"), ("b", "a")]),
        (["a", "b"], [("other", "a"), ("a", "a")]),
    ]
    report = {
        "reference": {
            "commit": "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f",
            "source": "hermes_workflows/engine.py",
            "source_sha256": hashlib.sha256(raw).hexdigest(),
        },
        "extraction": [
            {"text": text, "ids": namespace["_extract_task_ids_block"](text)} for text in extraction
        ],
        "ordering": [
            {"ids": ids, "links": links, "ordered": namespace["_topological_order"](ids, links)}
            for ids, links in orders
        ],
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
