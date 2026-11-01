"""Extract the pinned settings schema without importing upstream dependencies."""

import ast
import hashlib
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
upstream = root / ".upstream"
expected = "352e5ffb751e4798e66dcdf1dbe9a80dff768c0f"
actual = subprocess.check_output(
    ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
).strip()
if actual != expected:
    raise SystemExit("The upstream reference checkout is not pinned")
source = (upstream / "hermes_workflows/config.py").read_bytes()
tree = ast.parse(source)
schema = next(
    ast.literal_eval(node.value)
    for node in tree.body
    if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "SETTINGS_SCHEMA"
)
data = {
    "upstream_commit": expected,
    "source_sha256": hashlib.sha256(source).hexdigest(),
    "fields": {f["key"]: f for g in schema["groups"] for f in g["fields"]},
}
(root / "tests/fixtures/settings-reference.json").write_text(
    json.dumps(data, indent=2) + "\n", encoding="utf-8"
)
