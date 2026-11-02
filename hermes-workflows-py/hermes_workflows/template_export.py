"""Portable template bundles. Offline by default; structured bindings only."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .native import NativeHost
from .specs import require_valid

TEMPLATE_FORMAT = 1
GENERATOR_VERSION = 1
MAX_HINT_TIMEOUT = 120
WORKFLOWS_DOC = "https://github.com/hermegeddon/hermes-workflows-windows/tree/development/hermes-workflows-py"
MEMORY_DOC = "https://github.com/itechmeat/open-second-brain"


def _json(value, *, pretty=False):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      indent=2 if pretty else None, separators=None if pretty else (",", ":"))


def source_workflow(workflow):
    require_valid(workflow)
    return copy.deepcopy({k: v for k, v in workflow.items() if k not in ("ui", "template")})


def spec_sha(workflow):
    # JSON is also valid YAML and keeps stdlib-only export/import lossless.
    # Object key order and UI/provenance are presentation, not source identity.
    return "sha256:" + hashlib.sha256(_json(source_workflow(workflow)).encode()).hexdigest()


def cache_key(workflow, model=None, *, template_format=TEMPLATE_FORMAT,
              generator_version=GENERATOR_VERSION):
    for value in (template_format, generator_version):
        if type(value) is not int or value < 1:
            raise ValueError("template and generator versions must be positive integers")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("hints model must be a non-empty string")
    # An encoded tuple avoids delimiter collisions and distinguishes None from
    # an actual model named "none". The old snapshot cache has a separate prefix.
    return _json([workflow["id"], spec_sha(workflow), template_format, generator_version, model])


def clean_hints(value, workflow):
    if not isinstance(value, dict):
        return {}
    result = {}
    if isinstance(value.get("overview"), str) and value["overview"].strip():
        result["overview"] = value["overview"].strip()[:2000]
    ids = {node["id"] for node in workflow["nodes"]}
    nodes = []
    seen = set()
    for item in value.get("nodes", []) if isinstance(value.get("nodes"), list) else []:
        if (not isinstance(item, dict) or not isinstance(item.get("nodeId"), str)
                or item["nodeId"] not in ids or item["nodeId"] in seen):
            continue
        node = {"nodeId": item["nodeId"]}
        for key in ("role", "capability"):
            hint = item.get(key)
            if isinstance(hint, str) and hint.strip():
                # Hints are prose, never a nested placeholder or multiline token.
                node[key] = re.sub(r"[{}\r\n\x00-\x1f`]", " ", hint).strip()[:240]
        if len(node) > 1:
            nodes.append(node)
            seen.add(item["nodeId"])
    if nodes:
        result["nodes"] = nodes
    return result


def _hint(node, hints, key):
    supplied = next((v.get(key) for v in hints.get("nodes", []) if v["nodeId"] == node["id"]), None)
    label = node.get("title") or re.sub(r"[_-]+", " ", node["id"]).strip()
    value = supplied or (f'a worker suited to "{label}"' if key == "role"
                         else f'a model capable of "{label}"')
    return re.sub(r"[{}\r\n\x00-\x1f`]", " ", value).strip()[:240]


def debind(workflow, hints=None):
    wf = copy.deepcopy(workflow)
    hints = clean_hints(hints, wf)
    placeholders = []

    def replace(target, key, token, kind, field, *, node=None, hint=None):
        value = target.get(key)
        if value is None or "{{" in value or "${" in value:
            return
        entry = {"token": token, "kind": kind, "field": field, "original": value}
        if node is not None:
            entry["nodeId"] = node["id"]
        if hint is not None:
            entry["hint"] = hint
        placeholders.append(entry)
        target[key] = token

    projects = {}
    for project in wf["scope"].get("projects", []):
        if project not in projects:
            token = "${PROJECT}" if not projects else "${PROJECT_" + str(len(projects) + 1) + "}"
            projects[project] = token
            placeholders.append({"token": token, "kind": "project", "field": "scope.projects",
                                 "original": project})
    if projects:
        wf["scope"]["projects"] = [projects[p] for p in wf["scope"]["projects"]]
    defaults = wf.get("defaults", {})
    for key, kind, hint in (("profile", "profile", "the workflow's default worker role"),
                            ("model", "model", "the workflow's default model capability")):
        replace(defaults, key, "${" + kind.upper() + ":" + hint + "}", kind,
                "defaults." + key, hint=hint)
    replace(wf, "deliver", "${DELIVER_TARGET}", "deliver", "deliver")
    for node in wf["nodes"]:
        if node["type"] == "agent_task":
            for key, kind, hint in (("profile", "profile", _hint(node, hints, "role")),
                                    ("model", "model", _hint(node, hints, "capability")),
                                    ("review_profile", "profile", "a reviewer for " + node["id"])):
                replace(node, key, "${" + kind.upper() + ":" + hint + "}", kind,
                        "node." + key, node=node, hint=hint)
        replace(node, "workdir", "${WORKDIR}", "workdir", "node.workdir", node=node)
        if node["type"] == "agent_task":
            replace(node, "task_ref", "${TASK_REF}", "task_ref", "node.task_ref", node=node)
    return wf, placeholders


DETECTORS = [
    ("kanban_wrapper", r"hermes\s+kanban\b[^\n`'\"]*", "the kanban wrapper invocation"),
    ("kanban_wrapper", r"~/\.hermes/[^\s\"'`)]*", "a Hermes home path"),
    ("repo", r"github\.com[/:][\w.-]+/[\w.-]+", None),
    ("repo", r"git@github\.com:[\w.-]+/[\w.-]+", None),
    ("channel", r"\b(?:telegram|discord|slack|email):[\w:@.+-]+", None),
    ("channel", r"-100\d{6,}", "a Telegram supergroup id"),
    ("path", r"(?<=[\"'`])(?i:[A-Z]:[\\/]|\\\\)[^\"'`\r\n]+(?=[\"'`])", "a Windows path"),
    ("path", r"(?i:\b[A-Z]:[\\/])[^\s\"'`)]*|\\\\[^\s\"'`)]+", "a Windows path"),
    ("path", r"/(?:srv|root|tmp|usr|home|etc|opt|var)/[^\s\"'`)]*", None),
    ("url", r"https?://[^\s\"'`)]+", None),
]


def inventory(workflow):
    found = []
    seen = set()

    def add(node, field, kind, match, note=None):
        key = (node["id"], field, kind, match)
        if key in seen:
            return
        seen.add(key)
        item = dict(nodeId=node["id"], field=field, kind=kind, match=match)
        if note:
            item["note"] = note
        found.append(item)

    for node in workflow["nodes"]:
        for field in ("title", "description", "prompt", "command"):
            text = node.get(field)
            if not isinstance(text, str) or not text:
                continue
            claimed = []
            for kind, pattern, note in DETECTORS:
                for match in re.finditer(pattern, text, re.IGNORECASE if kind == "channel" else 0):
                    if any(match.start() < end and match.end() > start for start, end in claimed):
                        continue
                    claimed.append(match.span())
                    add(node, field, kind, match[0], note)
            for project in dict.fromkeys(workflow["scope"].get("projects", [])):
                if len(project) >= 3 and re.search(r"\b" + re.escape(project) + r"\b", text):
                    add(node, field, "project_name", project, "the source project name")
        for skill in node.get("skills", []):
            add(node, "skills", "skill", skill, "verify this skill exists on the target installation")
        if "branch" in node:
            add(node, "branch", "branch", node["branch"], "the stacked feature branch name")
        if node.get("wait_for"):
            add(node, "wait_for.github_pr_merged", "repo", node["wait_for"]["github_pr_merged"],
                "select the target pull request or repository context")
    return found


def generation_request(workflow):
    nodes = []
    for node in workflow["nodes"]:
        item = {k: node[k] for k in ("id", "type", "title", "description") if k in node}
        if node["type"] in ("agent_task", "prompt") and "prompt" in node:
            item["prompt"] = node["prompt"]
        elif node["type"] == "script":
            item["prompt"] = node["command"]
        if node["type"] == "agent_task":
            item.update({k: node[k] for k in ("profile", "model") if k in node})
        nodes.append(item)
    return {"workflow_id": workflow["id"], "spec_sha": spec_sha(workflow), "nodes": nodes}


def build_guide(workflow, placeholders, items, version, hints):
    lines = [f"# {workflow['name']} — workflow template", "",
             f"**Version:** `{version['human_version']}`", "",
             "## Prerequisites", "",
             f"- **REQUIRED:** Hermes Workflows Python v1.0.2 or later, template format 1. {WORKFLOWS_DOC}",
             f"- **RECOMMENDED:** OpenSecondBrain (o2b) for workflows that use its memory tools. {MEMORY_DOC}",
             "- Install the skills listed below and configure the required profiles/models on the target host.",
             "", "## How to instantiate", "",
             "1. Copy the `.template.yaml` file to a new workflow file. It uses JSON, a YAML subset, so no YAML dependency is required.",
             "2. Remove the `template` provenance block, choose a new workflow `id`, and replace every `${...}` placeholder with your own bindings.",
             "3. Review the original prompt/command references and skill/branch prerequisites below. Prose is preserved verbatim.",
             "4. Import with `hermes-workflows import <file>` and validate before running. For dashboard JSON import, wrap the adapted spec as `{\"workflow\": {...}}` in a `.workflow.json` file.", ""]
    if hints.get("overview"):
        lines += [hints["overview"], ""]
    if workflow.get("params"):
        lines += ["## Parameters", "", "Typed parameters are preserved. Review their defaults and enum options for the target installation.", ""]
        for param in workflow["params"]:
            lines.append(f"- `{param['name']}` · {param['type']}"
                         + (f" · default: `{_json(param['default'])}`" if "default" in param else ""))
        lines.append("")
    prerequisites = {"project": "a project/board on the target installation",
                     "profile": "a configured agent profile of the recommended type",
                     "model": "a configured provider/model of the recommended capability",
                     "deliver": "the intended delivery target, or origin/local",
                     "workdir": "a working directory on the target host",
                     "task_ref": "an existing Kanban card on the target board"}
    lines += ["## Workflow-level placeholders", ""]
    for item in placeholders:
        if "nodeId" not in item:
            lines.append(f"- `{item['token']}` · {item['field']}: {prerequisites[item['kind']]}.")
    lines += ["", "## Per-node placeholders & recommendations", ""]
    for node in workflow["nodes"]:
        ph = [v for v in placeholders if v.get("nodeId") == node["id"]]
        refs = [v for v in items if v["nodeId"] == node["id"]]
        if node["type"] != "agent_task" and not ph and not refs:
            continue
        lines += [f"### Node `{node['id']}` — {node['type']}", ""]
        if node["type"] == "agent_task":
            lines += [f"- Recommended profile type: {_hint(node, hints, 'role')}.",
                      f"- Recommended model capability: {_hint(node, hints, 'capability')}."]
        for item in ph:
            lines.append(f"- `{item['token']}` · {item['field']}: {prerequisites[item['kind']]}.")
        for item in refs:
            lines.append(f"- Adapt {item['field']} · {item['kind']}: `{item['match']}`"
                         + (f" — {item['note']}" if item.get("note") else ""))
        lines.append("")
    lines += ["## Source and generation", "",
              f"Source: `{version['source']['workflow_id']}` v{version['source']['workflow_version']} · `{version['source']['spec_sha']}`",
              f"Generated: {version['generation']['generated_at']} · generator v{version['generation']['generator_version']} · hints model: {version['generation']['model'] or 'none (offline)'}",
              "", "Structured bindings are removed; references in prose and skill/branch names remain visible for manual adaptation. Review both artifacts before sharing.", ""]
    return "\n".join(lines)


def build_bundle(workflow, *, generated_at, model=None, hints=None,
                 template_format=TEMPLATE_FORMAT, generator_version=GENERATOR_VERSION):
    wf = source_workflow(workflow)
    hints = clean_hints(hints, wf)
    key = cache_key(wf, model, template_format=template_format, generator_version=generator_version)
    revision = hashlib.sha256(key.encode()).hexdigest()[:8]
    version = {"template_format": template_format,
               "source": {"workflow_id": wf["id"], "workflow_version": wf["version"], "spec_sha": spec_sha(wf)},
               "generation": {"generator_version": generator_version, "model": model,
                              "generated_at": generated_at, "hints": "generated" if hints else "offline_fallback"},
               "revision": revision,
               "human_version": f"fmt{template_format}·wf{wf['version']}·r{revision[:4]}"}
    debound, placeholders = debind(wf, hints)
    items = inventory(wf)
    return {"id": wf["id"], "cached": False, "cache_key": key, "revision": revision,
            "human_version": version["human_version"], "spec_sha": version["source"]["spec_sha"],
            "version": version, "placeholders": placeholders, "inventory": items,
            "yaml_filename": wf["id"] + ".template.yaml",
            "yaml": _json({"template": version, **debound}, pretty=True) + "\n",
            "md_filename": wf["id"] + ".template.md",
            "md": build_guide(wf, placeholders, items, version, hints)}


def default_model(home):
    try:
        config = NativeHost(home).call("config", "load_config")
        model = config.get("model", {})
        if isinstance(model, str):
            return model or None
        name = model.get("default")
        if name and "@" not in name and model.get("provider"):
            name += "@" + model["provider"]
        return name or None
    except (ValueError, TypeError, AttributeError, RuntimeError, OSError):
        return None


def generate_hints(model, request, *, home, timeout):
    """One opt-in, tool-free model call in a bounded isolated Python worker."""
    from .executor import run_process

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return None  # The synchronous CLI owns this optional worker.
    with tempfile.TemporaryDirectory(prefix="hermes-template-") as directory:
        root = Path(directory)
        source, response = root / "request.json", root / "response.json"
        source.write_text(_json({"model": model, "request": request, "response": str(response),
                                 "timeout": timeout}), encoding="utf-8")
        env = dict(os.environ, HERMES_HOME=str(home), PYTHONIOENCODING="utf-8",
                   HERMES_DISABLE_LAZY_INSTALLS="1")
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
            [str(Path(__file__).resolve().parent.parent), *[str(Path(p).resolve()) for p in sys.path if p],
             env.get("PYTHONPATH", "")]))
        try:
            result = asyncio.run(run_process(
                [sys.executable, "-m", "hermes_workflows.template_hint_worker", str(source)],
                env=env, timeout=timeout, max_output=32_000))
            if result.outcome == "success" and response.is_file():
                return json.loads(response.read_text(encoding="utf-8"))
        except (OSError, ValueError, TimeoutError, RuntimeError):
            pass
    return None


def export(workflow, db, *, with_hints=False, model=None, generated_at=None,
           generator_version=GENERATOR_VERSION, hint_timeout=MAX_HINT_TIMEOUT,
           hint_runner=None):
    wf = source_workflow(workflow)
    if type(with_hints) is not bool:
        raise ValueError("with_hints must be boolean")
    if type(hint_timeout) not in (int, float) or not 0 < hint_timeout <= MAX_HINT_TIMEOUT:
        raise ValueError(f"hint timeout must be positive and at most {MAX_HINT_TIMEOUT}s")
    if model is not None and not with_hints:
        raise ValueError("select --with-hints before specifying a hints model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("hints model must be a non-empty string")
    chosen = (model or default_model(db.path.parent.parent)) if with_hints else None
    key = cache_key(wf, chosen, generator_version=generator_version)
    stored = db.export_cache("portable-template-v1:" + key)
    if stored:
        return {**stored, "cached": True}
    hints = None
    if chosen:
        try:
            hints = (hint_runner or generate_hints)(
                chosen, generation_request(wf), home=db.path.parent.parent, timeout=hint_timeout)
        except Exception:
            hints = None  # Optional advice cannot prevent the deterministic export.
    result = build_bundle(wf, generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
                          model=chosen, hints=hints, generator_version=generator_version)
    return db.export_cache_once("portable-template-v1:" + key, result)


def write_bundle(bundle, directory):
    """Materialize the cached pair, atomically replacing each owned artifact."""
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for kind in ("yaml", "md"):
        path = directory / bundle[kind + "_filename"]
        if path.parent != directory or path.name != bundle["id"] + ".template." + kind:
            raise ValueError("invalid template artifact filename")
        try:
            unchanged = path.read_bytes() == bundle[kind].encode("utf-8")
        except FileNotFoundError:
            unchanged = False
        if unchanged:
            files[kind] = str(path)
            continue
        # Named files in the same directory support os.replace on native Windows.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=directory,
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(bundle[kind])
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        files[kind] = str(path)
    return {**bundle, "files": files}
