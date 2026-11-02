"""Freeze pure template decisions from the pinned TS source using Node, never Bun."""

import argparse
import copy
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

PIN = "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f"


def cases():
    workflow = {
        "id": "portable-qa", "name": "Portable QA", "version": 3,
        "scope": {"type": "projects", "projects": ["widgets", "other", "widgets"]},
        "trigger": {"type": "manual"},
        "defaults": {"profile": "native-worker", "model": "source-model@source-provider"},
        "deliver": "discord:123:456",
        "params": [{"name": "count", "type": "int", "default": 3},
                   {"name": "enabled", "type": "bool", "default": False}],
        "nodes": [
            {"id": "plan", "type": "agent_task", "title": "Plan feature", "profile": "planner",
             "model": "reasoner@provider", "skills": ["architecture"],
             "prompt": "Clone github.com/acme/widgets under /srv/projects/widgets. "
                       "Run `hermes kanban complete t_x`. Notify telegram:-1009999:7."},
            {"id": "implement", "type": "agent_task", "profile": "engineer", "prompt": "Build widgets.",
             "adopt": True, "task_ref": "t_abc12345", "workdir": "/srv/projects/widgets", "stack": True,
             "branch": "feature/widgets"},
            {"id": "follow", "type": "agent_task", "prompt": "Follow up.", "adopt": True,
             "task_ref": "{{nodes.plan.output.task_ids}}"},
            {"id": "done", "type": "finish"}],
        "edges": [{"from": "plan", "to": "implement"}, {"from": "implement", "to": "follow"},
                  {"from": "follow", "to": "done"}],
    }
    hints = {"overview": "Plan and build a feature.",
             "nodes": [{"nodeId": "plan", "role": "experienced planner", "capability": "deep reasoning"}]}
    return [{"name": "offline", "workflow": workflow, "hints": None},
            {"name": "hints", "workflow": copy.deepcopy(workflow), "hints": hints}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=Path(".upstream"))
    parser.add_argument("--output", type=Path, default=Path("tests/fixtures/template-reference.json"))
    args = parser.parse_args()
    hashes = {}
    with tempfile.TemporaryDirectory(prefix="workflow-template-reference-") as temporary:
        root = Path(temporary)
        for path in ("packages/core/src/templates/exportTemplate.ts",
                     "packages/core/src/serialize/serializeWorkflow.ts",
                     "packages/core/src/serialize/specSha.ts"):
            raw = subprocess.check_output(["git", "-C", str(args.upstream), "show", f"{PIN}:{path}"])
            hashes[path] = hashlib.sha256(raw).hexdigest()
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            # Expose two pure private functions only in this temporary capture;
            # their pinned implementations remain unmodified.
            extra = b"\nexport { debindWorkflow, inventoryWorkflow };\n" if "exportTemplate.ts" in path else b""
            target.write_bytes(raw + extra)
        (root / "package.json").write_text('{"type":"module"}', encoding="utf-8")
        (root / "cases.json").write_text(json.dumps(cases()), encoding="utf-8")
        (root / "capture.mjs").write_text(
            "import fs from 'node:fs';\n"
            "import { debindWorkflow, inventoryWorkflow, generationRequest, templateCacheKey, templateRevision } "
            "from './packages/core/src/templates/exportTemplate.ts';\n"
            "const cases=JSON.parse(fs.readFileSync(new URL('./cases.json',import.meta.url),'utf8'));\n"
            "const result=cases.map(c=>({...c,expected:{debound:debindWorkflow(c.workflow,c.hints??undefined),"
            "inventory:inventoryWorkflow(c.workflow),nodes:generationRequest(c.workflow).nodes}}));\n"
            "const key=templateCacheKey('portable-qa','sha256:fixture',1,1,'hint-model@provider');\n"
            "process.stdout.write(JSON.stringify({cases:result,cache:{key,revision:templateRevision(key)}}));\n",
            encoding="utf-8")
        result = json.loads(subprocess.check_output(["node", str(root / "capture.mjs")], text=True, encoding="utf-8"))
    result = {"upstream_commit": PIN, "source_sha256": hashes, **result}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"Captured {len(result['cases'])} pinned template cases")


if __name__ == "__main__":
    main()
