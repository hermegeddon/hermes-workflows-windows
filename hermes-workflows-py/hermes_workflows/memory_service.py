"""Opt-in workflow policy and background dispatch; SQLite remains authoritative."""

from __future__ import annotations

import json
import re
import time

from . import settings
from .memory_adapter import HermesMemorySink
from .native import NativeHost, NativeUnavailable
from .workflow_memory import KIND, MemoryOutbox, MemoryPolicy, MemoryUnavailable


def policy(workflow, config):
    defaults = workflow.get("defaults", {})
    # Native policy is additive: never extend/reinterpret upstream's O2B enum.
    local = defaults.get("workflow_memory", defaults.get("memory", {}))
    if not isinstance(local, dict):
        return {"enabled": False, "reason": "invalid_policy"}
    enabled = local.get("enabled", config.get("workflow_memory_enabled", False))
    if enabled is False:
        return None
    if enabled is not True:
        return {"enabled": False, "reason": "invalid_policy"}
    provider = local.get("provider", config.get("workflow_memory_provider", "hermes"))
    if provider == "none":
        return None
    if provider != "hermes":
        return {"enabled": False, "reason": "unsupported_provider"}
    if local.get("fail_open", True) is not True:
        return {"enabled": False, "reason": "strict_mode_unsupported"}
    result = {"enabled": True}
    for key, default in (
        ("write_run_summaries", True),
        ("write_node_failures", True),
        ("write_node_events", False),
        ("allow_scheduled", False),
    ):
        value = local.get(key, config.get("workflow_memory_" + key, default))
        if type(value) is not bool:
            return {"enabled": False, "reason": "invalid_policy"}
        result[key] = value
    return (
        result
        if any(
            result[key]
            for key in ("write_run_summaries", "write_node_failures", "write_node_events")
        )
        else None
    )


class MemoryService:
    def __init__(self, db, *, context=None, sink_factory=HermesMemorySink):
        self.db = db
        self.home = db.path.parent.parent.resolve()
        self.context = context
        self.sink_factory = sink_factory
        self.outbox = MemoryOutbox(db)

    def sink(self, writer_context, *, config=None):
        config = settings.values(self.db) if config is None else config
        return self.sink_factory(
            self.home,
            writer_context,
            context=self.context,
            timeout=config.get("workflow_memory_timeout_seconds", 5),
        )

    @staticmethod
    def writer_context(run, workflow):
        routing = run.get("notification_context", {}).get("routing", {})
        origin = run.get("origin", "")
        platform = origin.partition(":")[0] if ":" in origin else "workflow"
        return {
            "session_id": run["run_id"],
            "platform": platform,
            "user_id": str(routing.get("user_id") or ""),
            "user_id_alt": str(routing.get("user_id_alt") or ""),
            "parent_session_id": str(routing.get("session_key") or ""),
            "cwd": workflow.get("defaults", {}).get("workdir", ""),
            "scheduled": run.get("trigger_context", {}).get("scheduled") is True,
        }

    def snapshot(self, home, *, run, workflow, **kwargs):
        config = kwargs.get("settings") or settings.values(self.db)
        selected = policy(workflow, config)
        if selected is None:
            return None
        blocked = {"version": 1, "enabled": False, "status": "unavailable"}
        if not selected["enabled"]:
            return {**blocked, "reason": selected["reason"]}
        writer_context = self.writer_context(run, workflow)
        if writer_context["scheduled"] and not selected["allow_scheduled"]:
            return {**blocked, "reason": "scheduled_records_disabled"}
        sink = self.sink(writer_context, config=config)
        try:
            destination = sink.bind()
        except MemoryUnavailable as exc:
            return {**blocked, "reason": exc.reason}
        except Exception:
            # The optional boundary must not expose credential-bearing errors.
            return {**blocked, "reason": "provider_unavailable"}
        return MemoryPolicy(
            destination,
            enabled=True,
            **{
                key: selected[key]
                for key in ("write_run_summaries", "write_node_failures", "write_node_events")
            },
            writer_context=sink.writer_context,
            capabilities=sink.capabilities,
        ).snapshot(home)

    async def dispatch(self):
        with self.db.connect() as con:
            if (
                con.execute("SELECT 1 FROM py_effects WHERE kind=? LIMIT 1", (KIND,)).fetchone()
                is None
            ):
                return
        self.outbox.recover_expired()
        with self.db.connect() as con:
            rows = con.execute(
                "SELECT effect_id,run_id,payload FROM py_effects WHERE kind=? "
                "AND status='prepared' ORDER BY created_at LIMIT 4",
                (KIND,),
            ).fetchall()
        if not rows:
            return
        config = settings.values(self.db)
        for row in rows:
            current = policy(self.db.get_spec(row["run_id"]), config)
            payload = json.loads(row["payload"])
            writer_context = payload.get("writer_context", {})
            scheduled_blocked = bool(
                current
                and current.get("enabled")
                and writer_context.get("scheduled")
                and not current["allow_scheduled"]
            )
            if not current or not current["enabled"] or scheduled_blocked:
                with self.db.connect(write=True) as con:
                    con.execute(
                        "UPDATE py_effects SET status='unavailable',result=?,updated_at=? "
                        "WHERE effect_id=? AND status='prepared' AND kind=?",
                        (
                            json.dumps(
                                {
                                    "reason": "scheduled_records_disabled"
                                    if scheduled_blocked
                                    else "policy_disabled"
                                }
                            ),
                            time.time(),
                            row["effect_id"],
                            KIND,
                        ),
                    )
                continue
            sink = self.sink(writer_context, config=config)
            await self.outbox.deliver(
                row["effect_id"], sink, timeout=config.get("workflow_memory_timeout_seconds", 5)
            )

    def records(self, run_id):
        self.db.get_run(run_id)
        return [
            {
                "effect_id": row["effect_id"],
                "revision": row["revision"],
                "status": row["status"],
                "record": row["payload"]["record"],
                "provider": row["payload"]["destination"]["provider"],
                "result": row["result"],
                "updated_at": row["updated_at"],
            }
            for row in self.outbox.entries(run_id)
        ]

    async def reconcile(self, run_id, effect_id):
        if not any(row["effect_id"] == effect_id for row in self.outbox.entries(run_id)):
            raise KeyError("memory effect not found for this run")
        row = self.outbox.effects.get(effect_id)
        sink = self.sink(row["payload"].get("writer_context", {}))
        await self.outbox.reconcile(effect_id, sink)
        return next(row for row in self.records(run_id) if row["effect_id"] == effect_id)

    def status(self):
        config = settings.values(self.db)
        metadata = self.sink({}, config=config).status()
        if metadata.get("provider") is None and not metadata.get("available"):
            try:
                value = (
                    NativeHost(self.home)
                    .call("config", "load_config")
                    .get("memory", {})
                    .get("provider")
                )
                if (
                    isinstance(value, str)
                    and value not in ("builtin", "default", "none")
                    and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value)
                ):
                    metadata["provider"] = value
            except (NativeUnavailable, ValueError, OSError):
                pass
        with self.db.connect() as con:
            counts = dict(
                con.execute(
                    "SELECT status,count(*) FROM py_effects WHERE kind=? GROUP BY status", (KIND,)
                )
            )
            row = con.execute(
                "SELECT run_id,status,payload,result,updated_at FROM py_effects "
                "WHERE kind=? ORDER BY updated_at DESC LIMIT 1",
                (KIND,),
            ).fetchone()
            bound = con.execute(
                "SELECT run_id,state,started_at FROM py_runs "
                "WHERE json_extract(state,'$.memory_export') IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        latest = None
        if row:
            latest = {
                "run_id": row["run_id"],
                "status": row["status"],
                "updated_at": row["updated_at"],
                "provider": json.loads(row["payload"])["destination"]["provider"],
            }
        capabilities = None
        if bound:
            context = json.loads(bound["state"])["memory_export"]
            capabilities = context.get("capabilities")
            if not context.get("enabled") and (
                row is None or bound["started_at"] > row["updated_at"]
            ):
                latest = {
                    "run_id": bound["run_id"],
                    "status": "unavailable",
                    "reason": context.get("reason"),
                }
        return {
            "default_enabled": config.get("workflow_memory_enabled", False),
            "route": config.get("workflow_memory_provider", "hermes"),
            "host": metadata,
            "last_verified_capabilities": capabilities,
            "counts": counts,
            "last_delivery": latest,
        }

    def preview(self, workflow):
        config = settings.values(self.db)
        selected = policy(workflow, config)
        defaults = workflow.get("defaults", {})
        local = defaults.get("workflow_memory", defaults.get("memory", {}))
        return {
            "route": local.get("provider", config.get("workflow_memory_provider", "hermes"))
            if isinstance(local, dict)
            else None,
            "enabled": bool(selected and selected["enabled"]),
            "fail_open": True,
            "provider_support": "unprobed",
            **({"reason": selected["reason"]} if selected and "reason" in selected else {}),
        }
