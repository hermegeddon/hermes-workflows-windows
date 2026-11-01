"""Settings schema consumed by the unchanged dashboard."""

from __future__ import annotations

import copy
import importlib
import logging
import os

from .db import Database
from .native import NativeHost, NativeUnavailable

SCHEMA = {
    "namespace": "plugins.workflows",
    "groups": [
        {
            "key": "notifications",
            "label": "Notifications",
            "fields": [
                {"key": "default_deliver", "type": "string", "default": "", "enforced": True},
            ],
        },
        {
            "key": "execution",
            "label": "Execution",
            "fields": [
                {"key": "scripts_enabled", "type": "bool", "default": False, "enforced": True},
                {"key": "script_env_allowlist", "type": "string", "default": "", "enforced": True},
                {
                    "key": "default_timeout_seconds",
                    "type": "int",
                    "default": 1800,
                    "enforced": True,
                },
                {"key": "max_parallel_runs", "type": "int", "default": 4, "enforced": True},
                {"key": "max_parallel_steps", "type": "int", "default": 4, "enforced": True},
                {"key": "max_loop_reentries", "type": "int", "default": 100, "enforced": True},
                {
                    "key": "default_shell",
                    "type": "enum",
                    "options": ["shell", "cmd", "powershell", "pwsh", "sh"],
                    "default": "shell",
                    "enforced": True,
                },
            ],
        },
        {
            "key": "observability",
            "label": "Observability",
            "fields": [
                {"key": "trace_enabled", "type": "bool", "default": False, "enforced": True},
                {
                    "key": "worker_approvals_enabled",
                    "type": "bool",
                    "default": False,
                    "enforced": True,
                },
            ],
        },
    ],
}

# Preserve the Python engine's established defaults. Settings for remaining
# parity milestones are persisted, but never advertised as enforced yet.
SCHEMA["groups"].extend(
    [
        {
            "key": "storage",
            "label": "Storage",
            "fields": [
                {"key": "global_workflows_path", "type": "string", "enforced": False},
                {"key": "runs_db_path", "type": "string", "enforced": False},
            ],
        },
        {
            "key": "scheduling",
            "label": "Scheduling",
            "fields": [
                {
                    "key": "scheduler_backend",
                    "type": "enum",
                    "options": ["auto", "native", "standalone"],
                    "default": "auto",
                    "enforced": True,
                },
            ],
        },
        {
            "key": "kanban",
            "label": "Kanban",
            "fields": [
                {
                    "key": "use_workflow_columns",
                    "type": "enum",
                    "options": ["auto", "on", "off"],
                    "default": "auto",
                    "enforced": False,
                },
                {
                    "key": "internal_board",
                    "type": "string",
                    "default": "hermes-workflows",
                    "env": "HERMES_WORKFLOWS_BOARD",
                    "enforced": False,
                },
            ],
        },
        {
            "key": "dashboard",
            "label": "Dashboard",
            "fields": [
                {
                    "key": "dashboard_api_host",
                    "type": "string",
                    "default": "127.0.0.1",
                    "env": "HERMES_WORKFLOWS_DASHBOARD_API_HOST",
                    "enforced": False,
                },
                {
                    "key": "dashboard_api_port",
                    "type": "int",
                    "default": 9123,
                    "env": "HERMES_WORKFLOWS_DASHBOARD_API_PORT",
                    "enforced": False,
                },
            ],
        },
        {
            "key": "open_second_brain",
            "label": "OpenSecondBrain",
            "fields": [
                {
                    "key": "mode",
                    "type": "enum",
                    "options": ["auto", "open_second_brain", "none"],
                    "default": "auto",
                    "enforced": False,
                },
                {"key": "fail_open", "type": "bool", "default": True, "enforced": False},
                *[
                    {"key": key, "type": "bool", "default": default, "enforced": False}
                    for key, default in (
                        ("write_run_summaries", True),
                        ("write_node_failures", True),
                        ("write_node_events", False),
                    )
                ],
            ],
        },
    ]
)
SCHEMA["groups"][1]["fields"].extend(
    [
        {
            "key": "default_mode",
            "type": "enum",
            "options": ["durable", "direct"],
            "default": "durable",
            "enforced": False,
        },
        {"key": "event_debounce_seconds", "type": "int", "default": 2, "enforced": False},
        {"key": "tick_schedule", "type": "string", "default": "every 2m", "enforced": False},
    ]
)
for group in SCHEMA["groups"]:
    for field in group["fields"]:
        if field["key"] == "trace_enabled":
            field["env"] = "HERMES_WORKFLOWS_TRACE"
        elif field["key"] == "default_deliver":
            field["env"] = "HERMES_WORKFLOWS_DELIVER"


def schema(db: Database) -> dict:
    result = copy.deepcopy(SCHEMA)
    home = db.path.parent.parent.resolve()
    for group in result["groups"]:
        for field in group["fields"]:
            if field["key"] in ("global_workflows_path", "runs_db_path"):
                field["default"] = str(
                    home / "workflows/global"
                    if field["key"] == "global_workflows_path"
                    else db.path
                )
    return result


def validate(patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise ValueError("settings must be an object")
    fields = {f["key"]: f for g in SCHEMA["groups"] for f in g["fields"]}
    cleaned = {}
    for key, value in patch.items():
        field = fields.get(key)
        if field is None:
            raise ValueError(f"unknown setting: {key}")
        kind = field["type"]
        if kind == "bool" and isinstance(value, str):
            raw = value.strip().lower()
            value = (
                True
                if raw in ("true", "1", "yes", "on")
                else False
                if raw in ("false", "0", "no", "off")
                else value
            )
        if kind == "int" and isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        if (
            kind == "bool"
            and type(value) is not bool
            or kind == "int"
            and (type(value) is not int or value < 1)
        ):
            raise ValueError(f"invalid value for {key}")
        if kind in ("string", "enum") and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if kind == "enum" and value not in field["options"]:
            raise ValueError(f"invalid option for {key}")
        if (
            key in ("max_parallel_runs", "max_parallel_steps")
            and value > 128
            or key == "dashboard_api_port"
            and value > 65535
        ):
            raise ValueError(f"{key} exceeds its maximum")
        cleaned[key] = value
    return cleaned


class Settings:
    def __init__(self, db, *, host=None):
        self.db = db
        if host is None:
            try:
                importlib.import_module("hermes_cli.config")
            except ModuleNotFoundError as exc:
                if exc.name not in ("hermes_cli", "hermes_cli.config"):
                    raise NativeUnavailable("Hermes config dependencies are unavailable") from exc
            else:
                host = NativeHost(db.path.parent.parent)
        self.host = host

    def stored(self):
        if self.host is None:
            return {}
        cfg = self.host.call("config", "load_config")
        plugins = cfg.get("plugins", {})
        if not isinstance(plugins, dict) or not isinstance(plugins.get("workflows", {}), dict):
            raise ValueError("plugins.workflows must be an object")
        return plugins.get("workflows", {})

    def values(self):
        stored, legacy = self.stored(), self.db.settings()
        names = {f["env"] for g in SCHEMA["groups"] for f in g["fields"] if f.get("env")}
        environment = self.host.env_settings(names) if self.host else os.environ
        result = {}
        for group in schema(self.db)["groups"]:
            for field in group["fields"]:
                key = field["key"]
                if key in stored or key in legacy:
                    value = stored[key] if key in stored else legacy[key]
                    result[key] = validate({key: value})[key]
                else:
                    raw = environment.get(field.get("env", ""))
                    try:
                        result[key] = (
                            validate({key: raw})[key] if raw is not None else field["default"]
                        )
                    except ValueError:
                        logging.getLogger(__name__).warning(
                            "Ignoring invalid environment override for %s", key
                        )
                        result[key] = field["default"]
        return result

    def save(self, patch):
        cleaned = validate(patch)
        if self.host is None:
            self.db.save_settings(cleaned)
        else:
            # Serialize this plugin's writers across embedded hosts. Persist
            # only edited keys: copying legacy values into an unrelated patch
            # could overwrite another host's newer choice. Unedited legacy
            # values remain a compatibility fallback, never materialized defaults.
            with self.db.connect(write=True):
                self.host.call(
                    "config",
                    "save_config",
                    {"plugins": {"workflows": cleaned}},
                    merge_existing=True,
                )
                persisted = self.stored()
                if any(persisted.get(k) != value for k, value in cleaned.items()):
                    raise NativeUnavailable(
                        "Hermes did not persist the settings patch; configuration may be read-only"
                    )
        return self.values()


def values(db: Database) -> dict:
    return Settings(db).values()


def save(db: Database, patch: dict) -> dict:
    return Settings(db).save(patch)
