"""Settings schema consumed by the unchanged dashboard."""

from __future__ import annotations

from .db import Database

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
            ],
        },
    ],
}


def values(db: Database) -> dict:
    return {
        **{f["key"]: f["default"] for g in SCHEMA["groups"] for f in g["fields"]},
        **db.settings(),
    }


def save(db: Database, patch: dict) -> dict:
    fields = {f["key"]: f for g in SCHEMA["groups"] for f in g["fields"]}
    for key, value in patch.items():
        field = fields.get(key)
        if field is None:
            raise ValueError(f"unknown setting: {key}")
        kind = field["type"]
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
        if key in ("max_parallel_runs", "max_parallel_steps") and value > 128:
            raise ValueError(f"{key} must be at most 128")
    db.save_settings(patch)
    return values(db)
