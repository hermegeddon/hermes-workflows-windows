"""Workflow files, validation and the dashboard's compile-preview contract."""

from __future__ import annotations

import copy
import json
import math
import re
import threading
import uuid
from pathlib import Path

from .adoption import TASK_REF
from .db import ConflictError, hermes_home
from .graph import analyse, ancestors, predecessors

SLUG = re.compile(r"[A-Za-z0-9_-]+\Z")
REF = re.compile(
    r"\{\{(nodes\.[A-Za-z0-9_-]+\.(?:output(?:\.[A-Za-z0-9_.-]+)?|review_note)|params\.[A-Za-z0-9_-]+)\}\}"
)
REVIEWS = ("approved", "rejected", "needs_changes")
NODE_TYPES = {"agent_task", "script", "condition", "human_review", "finish", "prompt", "wait"}


def parse_document(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # Optional: already supplied by Hermes, never a core dependency.
        except ImportError as exc:
            raise ValueError(
                "YAML requires the Hermes environment's PyYAML; JSON works with the standard library"
            ) from exc
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("workflow document must be an object")
    return value


def dependencies(workflow: dict) -> dict[str, set[str]]:
    return predecessors(workflow)


def validate(workflow: dict) -> dict:
    errors, warnings = [], []

    def err(code, message):
        errors.append({"level": "error", "code": code, "message": message})

    if not isinstance(workflow, dict):
        err("invalid_workflow", "workflow must be an object")
        return {"valid": False, "errors": errors, "warnings": warnings}
    if not isinstance(workflow.get("id"), str) or not SLUG.fullmatch(workflow["id"]):
        err("invalid_id", "workflow id must contain only letters, digits, underscores and hyphens")
    if not isinstance(workflow.get("name"), str) or not workflow["name"].strip():
        err("invalid_name", "name must be a non-empty string")
    if type(workflow.get("version")) is not int or workflow["version"] < 1:
        err("invalid_version", "version must be a positive integer")
    if "enabled" in workflow and type(workflow["enabled"]) is not bool:
        err("invalid_enabled", "enabled must be boolean")
    if "deliver" in workflow and (
        not isinstance(workflow["deliver"], str) or not workflow["deliver"].strip()
    ):
        err("invalid_delivery", "deliver must be a non-empty string")
    notifications = workflow.get("notifications", {})
    if not isinstance(notifications, dict) or (
        "subscribe_cards" in notifications and type(notifications["subscribe_cards"]) is not bool
    ):
        err("invalid_notifications", "notifications.subscribe_cards must be boolean")
    scope = workflow.get("scope", {})
    if not isinstance(scope, dict) or scope.get("type") not in ("global", "project", "projects"):
        err("invalid_scope", "scope.type must be global, project or projects")
    elif "projects" in scope and (
        not isinstance(scope["projects"], list)
        or not all(isinstance(p, str) and SLUG.fullmatch(p) for p in scope["projects"])
    ):
        err("invalid_scope", "scope.projects must contain project slugs")
    defaults = workflow.get("defaults", {})
    if not isinstance(defaults, dict):
        err("invalid_defaults", "defaults must be an object")
        defaults = {}
    for key in ("profile", "model"):
        if key in defaults and not isinstance(defaults[key], str):
            err("invalid_defaults", f"defaults.{key} must be a string")
    if "max_retries" in defaults and (
        type(defaults["max_retries"]) is not int or not 0 <= defaults["max_retries"] <= 100
    ):
        err("invalid_retries", "defaults.max_retries must be an integer from 0 to 100")
    nodes, edges = workflow.get("nodes"), workflow.get("edges")
    if not isinstance(nodes, list) or not nodes or not all(isinstance(n, dict) for n in nodes):
        err("invalid_nodes", "nodes must be a non-empty list of objects")
        nodes = []
    if not isinstance(edges, list) or not all(isinstance(e, dict) for e in edges):
        err("invalid_edges", "edges must be a list of objects")
        edges = []
    seen = set()
    for node in nodes:
        name = node.get("id")
        if not isinstance(name, str) or not SLUG.fullmatch(name):
            err("invalid_node_id", "node id must be a slug")
            continue
        if name in seen:
            err("duplicate_node", f"duplicate node id '{name}'")
        seen.add(name)
        kind = node.get("type")
        if "notify_completion" in node and type(node["notify_completion"]) is not bool:
            err("invalid_notifications", f"'{name}'.notify_completion must be boolean")
        if not isinstance(kind, str) or kind not in NODE_TYPES:
            err("invalid_node_type", f"unknown node type for '{name}'")
        if kind == "script" and (
            not isinstance(node.get("command"), str) or not node["command"].strip()
        ):
            err("empty_command", f"script '{name}' requires a command")
        if kind == "agent_task":
            for field in ("adopt", "sequential", "stack"):
                if field in node and type(node[field]) is not bool:
                    err("invalid_adopt", f"'{name}'.{field} must be boolean")
            for field in ("task_ref", "review_profile"):
                if field in node and (not isinstance(node[field], str) or not node[field].strip()):
                    err("invalid_adopt", f"'{name}'.{field} must be a non-empty string")
                if field in node and node.get("adopt") is not True:
                    err(f"{field}_without_adopt", f"'{name}'.{field} requires adopt:true")
            if node.get("sequential") is True and node.get("adopt") is not True:
                err("sequential_without_adopt", f"'{name}'.sequential requires adopt:true")
            if node.get("adopt") is True:
                ref = node.get("task_ref")
                if not isinstance(ref, str) or not (SLUG.fullmatch(ref) or TASK_REF.fullmatch(ref)):
                    err(
                        "invalid_task_ref",
                        f"'{name}' needs a literal card id or a typed task_ids reference",
                    )
                if node.get("board") is False:
                    err("adopt_off_board", f"'{name}': adopt requires native board execution")
            if "board" in node and type(node["board"]) is not bool:
                err("invalid_board", f"'{name}'.board must be boolean")
            if not isinstance(node.get("prompt"), str):
                err("invalid_prompt", f"agent_task '{name}' requires a prompt string")
            if not (node.get("profile") or defaults.get("profile")):
                err("missing_profile", f"agent_task '{name}' has no profile or defaults.profile")
            workspace = node.get("workspace", {})
            if not isinstance(workspace, dict):
                err("invalid_workspace", f"'{name}' workspace must be an object")
                workspace = {}
            if "workspace" in node and workspace.get("type") not in ("scratch", "worktree"):
                err("invalid_workspace", f"'{name}': workspace.type must be scratch or worktree")
            if node.get("stack") and node.get("adopt") is not True:
                err("stack_without_adopt", f"'{name}': stack requires adopt:true")
            if "branch" in node and (
                not isinstance(node["branch"], str) or not node["branch"].strip()
            ):
                err("invalid_branch", f"'{name}': branch must be a non-empty string")
            if "branch" in node and not node.get("stack"):
                err("branch_without_stack", f"'{name}': branch requires stack:true")
            if (node.get("stack") or workspace.get("type") == "worktree") and node.get(
                "board"
            ) is False:
                err("worktree_off_board", f"'{name}': worktrees require native board execution")
        for key in ("profile", "model", "workdir", "title", "description"):
            if key in node and not isinstance(node[key], str):
                err("invalid_field", f"{name}.{key} must be a string")
        if "timeout_seconds" in node and (
            type(node["timeout_seconds"]) not in (int, float)
            or not math.isfinite(node["timeout_seconds"])
            or node["timeout_seconds"] <= 0
        ):
            err("invalid_timeout", f"'{name}' timeout_seconds must be positive")
        if "max_retries" in node and (
            type(node["max_retries"]) is not int or not 0 <= node["max_retries"] <= 100
        ):
            err("invalid_retries", f"'{name}' max_retries must be an integer from 0 to 100")
        for key in ("skills", "env"):
            if key in node and (
                not isinstance(node[key], list) or not all(isinstance(v, str) for v in node[key])
            ):
                err("invalid_field", f"{name}.{key} must be a list of strings")
        for key in ("enabled_toolsets", "disabled_toolsets"):
            if key in node and (
                not isinstance(node[key], list) or not all(isinstance(v, str) for v in node[key])
            ):
                err("invalid_field", f"{name}.{key} must be a list of strings")
        for key in ("max_iterations", "max_tokens"):
            if key in node and (type(node[key]) is not int or node[key] < 1):
                err("invalid_execution_limit", f"{name}.{key} must be a positive integer")
        if "load_external_secrets" in node and type(node["load_external_secrets"]) is not bool:
            err("invalid_execution_option", f"{name}.load_external_secrets must be boolean")
        if kind == "script" and node.get("shell", "shell") not in (
            "shell",
            "cmd",
            "powershell",
            "pwsh",
            "sh",
        ):
            err("invalid_shell", f"unknown shell for '{name}'")
        if kind == "human_review":
            options = node.get("options", list(REVIEWS))
            if (
                not isinstance(options, list)
                or not options
                or any(o not in REVIEWS for o in options)
            ):
                err("invalid_review_options", f"invalid options for '{name}'")
        if kind == "finish" and node.get("outcome", "success") not in ("success", "failure"):
            err("invalid_outcome", f"invalid finish outcome for '{name}'")
        if kind == "wait":
            wait = node.get("wait_for")
            if (
                not isinstance(wait, dict)
                or set(wait) != {"github_pr_merged"}
                or not isinstance(wait.get("github_pr_merged"), str)
                or not wait.get("github_pr_merged", "").strip()
            ):
                err("invalid_wait", f"'{name}' requires wait_for.github_pr_merged")
        mapping = node.get("input_mapping", {})
        if not isinstance(mapping, dict) or any(not isinstance(v, str) for v in mapping.values()):
            err(
                "invalid_input_mapping",
                f"'{name}' input_mapping must map names to reference strings",
            )
        elif kind == "agent_task":
            for key, ref in mapping.items():
                match = REF.fullmatch(ref)
                if (
                    not isinstance(key, str)
                    or not SLUG.fullmatch(key)
                    or not match
                    or not ref.startswith("{{nodes.")
                ):
                    err(
                        "invalid_input_mapping",
                        f"'{name}' input_mapping needs named node-channel references",
                    )
                elif ".review_note" in ref:
                    source = match[1].split(".")[1]
                    if any(
                        n.get("id") == source and n.get("type") != "human_review" for n in nodes
                    ):
                        err(
                            "review_note_source",
                            f"'{name}' review_note source must be a human_review",
                        )
    safe_edges = []
    for edge in edges:
        if (
            not isinstance(edge.get("from"), str)
            or not isinstance(edge.get("to"), str)
            or edge["from"] not in seen
            or edge["to"] not in seen
        ):
            err("unknown_edge_node", "edge endpoints must reference existing nodes")
            continue
        safe_edges.append(edge)
        condition = edge.get("condition")
        if condition is not None:
            if not isinstance(condition, dict):
                err("invalid_condition", "condition must be an object")
            elif condition.get("type") == "node_status":
                if (
                    not isinstance(condition.get("node"), str)
                    or condition.get("node") not in seen
                    or condition.get("equals")
                    not in (
                        "success",
                        "failure",
                    )
                ):
                    err(
                        "invalid_condition",
                        "node_status needs an existing node and success/failure",
                    )
            elif condition.get("type") == "review_status":
                source = next(n for n in nodes if n.get("id") == edge["from"])
                if condition.get("equals") not in REVIEWS or source.get("type") != "human_review":
                    err(
                        "invalid_condition",
                        "review_status must originate at a human_review and use an allowed decision",
                    )
            else:
                err(
                    "invalid_condition",
                    "only node_status and review_status conditions are supported",
                )
        if "fallback" in edge and type(edge["fallback"]) is not bool:
            err("invalid_fallback", "fallback must be boolean")
    if len(seen) == len(nodes) and nodes:
        graph, _, feedback, roots, reachable = analyse({"nodes": nodes, "edges": safe_edges})
        if feedback:
            warnings.append(
                {
                    "level": "warning",
                    "code": "cycle",
                    "message": "Workflow contains a cycle; loop re-entries are bounded by max_loop_reentries.",
                }
            )
            if not any(n.get("type") == "finish" for n in nodes):
                err("no_finish", "cyclic workflows require a finish node")
        if len(roots) != 1:
            err("entry_count", "exactly one entry node is required")
        lineage = {name: ancestors(graph, name) for name in graph}
        if len(roots) == 1:
            for name in graph.keys() - reachable:
                err("unreachable_node", f"'{name}' is unreachable from the entry node")
        params = workflow.get("params", [])
        param_names = (
            {
                p.get("name")
                for p in params
                if isinstance(p, dict) and isinstance(p.get("name"), str)
            }
            if isinstance(params, list)
            else set()
        )
        for node in nodes:
            name = node["id"]
            if node.get("type") == "finish" and any(e["from"] == name for e in safe_edges):
                err("finish_has_outgoing", f"finish '{name}' must be terminal")
            for match in REF.finditer(json.dumps(node)):
                parts = match[1].split(".")
                if parts[0] == "nodes" and parts[1] not in lineage.get(name, set()):
                    err(
                        "invalid_input_ref",
                        f"'{name}' references a node that is not an ancestor: {parts[1]}",
                    )
                if parts[0] == "params" and parts[1] not in param_names:
                    err("unknown_param_ref", f"unknown param '{parts[1]}'")
        for edge in safe_edges:
            c = edge.get("condition")
            if (
                isinstance(c, dict)
                and c.get("type") == "node_status"
                and isinstance(c.get("node"), str)
                and c.get("node") not in lineage.get(edge["from"], set()) | {edge["from"]}
            ):
                err(
                    "invalid_condition_ref", "condition node must be the edge source or an ancestor"
                )
    trigger = workflow.get("trigger", {})
    if not isinstance(trigger, dict) or trigger.get("type") not in (
        "manual",
        "cron",
        "webhook",
        "github",
        "api",
    ):
        err("invalid_trigger", "invalid trigger type")
    elif trigger["type"] == "cron":
        try:
            from .schedules import Cron

            Cron(trigger.get("schedule", ""), trigger.get("timezone", "UTC"))
        except (ValueError, TypeError) as exc:
            err("invalid_cron", str(exc))
    elif trigger["type"] != "manual":
        warnings.append(
            {
                "level": "warning",
                "code": "manual_event_trigger",
                "message": "Event triggers are declarative; start runs through the API or Hermes tools.",
            }
        )
    try:
        fill_params(workflow, {}, require=False)
    except ValueError as exc:
        err("invalid_params", str(exc))
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def require_valid(workflow: dict):
    result = validate(workflow)
    if not result["valid"]:
        raise ValueError("; ".join(e["message"] for e in result["errors"]))


def fill_params(workflow: dict, supplied: dict, *, require=True) -> dict:
    if not isinstance(supplied, dict):
        raise ValueError("params must be an object")
    definitions = workflow.get("params", [])
    if not isinstance(definitions, list) or not all(isinstance(p, dict) for p in definitions):
        raise ValueError("params must be a list of parameter objects")
    names = [p.get("name") for p in definitions]
    if any(not isinstance(n, str) or not SLUG.fullmatch(n) for n in names) or len(
        set(names)
    ) != len(names):
        raise ValueError("param names must be unique slugs")
    if set(supplied) - set(names):
        raise ValueError("unknown params: " + ", ".join(set(supplied) - set(names)))
    values = {}
    for param in definitions:
        name, kind = param["name"], param.get("type")
        if kind not in ("text", "enum", "int", "bool"):
            raise ValueError(f"unknown param type for {name}")
        if kind == "enum" and (
            not isinstance(param.get("options"), list)
            or not param["options"]
            or not all(isinstance(v, str) for v in param["options"])
        ):
            raise ValueError(f"{name} enum needs string options")
        value = supplied.get(name, param.get("default"))
        if value is None or value == "":
            if require and not param.get("optional", False):
                raise ValueError(f"missing required param: {name}")
            continue
        if kind == "int":
            if type(value) is int or isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
                value = int(value)
            else:
                raise ValueError(f"{name} must be an integer")
        elif kind == "bool":
            if isinstance(value, str) and value.lower() in ("true", "false"):
                value = value.lower() == "true"
            if type(value) is not bool:
                raise ValueError(f"{name} must be boolean")
        elif not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if kind == "enum" and param.get("strict", True) and value not in param["options"]:
            raise ValueError(f"{name} must be one of {param['options']}")
        values[name] = value
    return values


def resolve(text: str, run: dict, inputs: dict | None = None) -> str:
    def replacement(match):
        parts = match[1].split(".")
        if parts[0] == "params":
            if parts[1] not in run.get("params", {}):
                raise ValueError(f"run has no value for param '{parts[1]}'")
            value = run["params"][parts[1]]
        elif parts[0] != "nodes":
            return inputs.get(parts[0], match[0]) if inputs else match[0]
        else:
            state = run["nodes"][parts[1]]
            if state["status"] != "completed" or parts[2] not in state or state[parts[2]] is None:
                raise ValueError(
                    f"node '{parts[1]}' has no available {parts[2]} in its current iteration"
                )
            value = state[parts[2]]
            if len(parts) > 3:
                remaining = parts[3:]
                if remaining[0] == "task_ids" and "task_ids" in state:
                    value, remaining = state["task_ids"], remaining[1:]
                elif isinstance(value, str):
                    value = json.loads(value)
                for part in remaining:
                    value = value[part]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    # One pass over authored text: injected outputs containing another token
    # must never be interpreted as a second instruction/substitution.
    if inputs:
        pattern = re.compile(REF.pattern + r"|\{\{([A-Za-z0-9_-]+)\}\}")

        def combined(match):
            if match[1] is None:
                return inputs.get(match[2], match[0])
            return replacement(match)

        return pattern.sub(combined, text)
    return REF.sub(replacement, text)


class SpecStore:
    def __init__(self, roots: list[Path] | None = None):
        self.roots = (
            [Path(p).expanduser().resolve() for p in roots]
            if roots
            else [hermes_home() / "workflows" / "global", hermes_home() / "workflows" / "templates"]
        )
        self._lock = threading.RLock()

    def all(self) -> list[dict]:
        found = {}
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.suffix.lower() not in (".json", ".yaml", ".yml") or not path.is_file():
                    continue
                try:
                    value = parse_document(path.read_text(encoding="utf-8-sig"))
                    wf = value.get("workflow", value)
                    if not isinstance(wf, dict) or not isinstance(wf.get("id"), str):
                        continue
                    ui = value.get("ui")
                    wf = {k: v for k, v in wf.items() if k != "ui"}
                    detail = {"workflow": wf, "path": str(path.resolve())}
                    if ui is not None:
                        detail["ui"] = ui
                    found.setdefault(wf["id"], detail)
                except (OSError, ValueError):
                    continue
        return list(found.values())

    def get(self, workflow_id: str) -> dict:
        for detail in self.all():
            if detail["workflow"]["id"] == workflow_id:
                return detail
        raise KeyError("workflow not found")

    def save(self, payload: dict, *, create=False) -> dict:
        wf = payload.get("workflow")
        require_valid(wf)
        with self._lock:
            try:
                old = self.get(wf["id"])
            except KeyError:
                old = None
            if old and create:
                raise ConflictError("workflow already exists")
            if old:
                path = Path(old["path"])
            else:
                root = (
                    self.roots[-1]
                    if wf["scope"]["type"] == "projects" and len(self.roots) > 1
                    else self.roots[0]
                )
                root.mkdir(parents=True, exist_ok=True)
                path = root / (wf["id"] + ".workflow.json")
            document = copy.deepcopy(wf)
            if payload.get("ui") is not None:
                if not isinstance(payload["ui"], dict):
                    raise ValueError("ui must be an object")
                document["ui"] = payload["ui"]
            # JSON is valid YAML 1.2, so existing .yaml files remain importable.
            text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
            if create:
                try:
                    with path.open("x", encoding="utf-8", newline="\n") as handle:
                        handle.write(text)
                except FileExistsError as exc:
                    raise ConflictError("workflow already exists") from exc
            else:
                temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    temporary.write_text(text, encoding="utf-8")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            return {
                "workflow": copy.deepcopy(wf),
                "path": str(path.resolve()),
                **({"ui": payload["ui"]} if "ui" in payload else {}),
            }

    def delete(self, workflow_id: str):
        with self._lock:
            Path(self.get(workflow_id)["path"]).unlink()


def compile_preview(workflow: dict, *, mode="durable", project_id=None) -> dict:
    require_valid(workflow)
    from .kanban import board_for, on_board

    if mode not in ("durable", "direct"):
        raise ValueError("mode must be durable or direct")
    board = board_for(workflow, project_id)
    defaults = workflow.get("defaults", {})
    tasks = []
    for n in workflow["nodes"]:
        if n["type"] == "agent_task":
            tasks.append(
                {
                    "node": n["id"],
                    "kind": "agent",
                    "executor": "kanban" if on_board(n, board) else "direct",
                    "assignee": n.get("profile", defaults.get("profile", "")),
                    "workflow_template_id": workflow["id"],
                    "current_step_key": n["id"],
                    "prompt": n["prompt"],
                    **{k: defaults[k] for k in ("model", "max_retries") if k in defaults and k not in n},
                    **(
                        {"off_board": True}
                        if not on_board(n, board)
                        else {}
                    ),
                    **{
                        k: n[k]
                        for k in (
                            "title",
                            "model",
                            "skills",
                            "workdir",
                            "timeout_seconds",
                            "max_retries",
                            "input_mapping",
                            "adopt",
                            "task_ref",
                            "review_profile",
                            "sequential",
                            "stack",
                            "branch",
                            "notify_completion",
                        )
                        if k in n
                    },
                    **({"workspace": n["workspace"]["type"]} if "workspace" in n else {}),
                }
            )
    _, _, _, roots, _ = analyse(workflow)
    trigger = workflow["trigger"]
    result = {
        "workflow_id": workflow["id"],
        "scope": workflow["scope"],
        "trigger": trigger,
        "first_node": roots[0],
        "execution": {"mode": mode, "inline_scripts": mode == "direct"},
        "kanban_tasks": tasks,
        "script_steps": [
            {
                "node": n["id"],
                "kind": "script",
                "executor": "script",
                **{k: v for k, v in n.items() if k not in ("id", "type")},
            }
            for n in workflow["nodes"]
            if n["type"] == "script"
        ],
        "wait_steps": [
            {"node": n["id"], "kind": "wait", "wait_for": n["wait_for"]}
            for n in workflow["nodes"]
            if n["type"] == "wait"
        ],
        "cron_jobs": [
            {
                "schedule": trigger["schedule"],
                "timezone": trigger.get("timezone", "UTC"),
                "command": f"hermes-workflows run {workflow['id']}",
            }
        ]
        if trigger["type"] == "cron"
        else [],
        "profiles": sorted({t["assignee"] for t in tasks}),
        "skills": sorted({s for t in tasks for s in t.get("skills", [])}),
        "subscribe_cards": workflow.get("notifications", {}).get("subscribe_cards", True),
        "memory": {"provider": "none", "fail_open": True},
    }
    if workflow.get("params"):
        command = (
            "/workflow run "
            + workflow["id"]
            + " "
            + " ".join(p["name"] + "=<value>" for p in workflow["params"])
        )
        result.update(
            params=workflow["params"],
            catalog={
                "fields": workflow["params"],
                "command": command,
                "appUrl": "hermes://workflow/" + workflow["id"],
            },
        )
    if workflow.get("deliver"):
        result["deliver"] = workflow["deliver"]
    return result
