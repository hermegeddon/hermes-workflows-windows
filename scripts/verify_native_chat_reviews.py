"""Verify the installed gateway hook and router with temporary, capture-only chats.

Uses the real directory loader, plugin dispatcher, event/source classes, pending
control store and delivery router. Authorization is an injected policy seam;
there is no messaging account, provider or production gateway operation.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_native_host import activate


async def verify(home, plugin):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.delivery import DeliveryRouter
    from gateway.platforms.event import MessageEvent
    from gateway.run_inbound_hooks import GatewayInboundHooksMixin
    from gateway.session import SessionSource
    from hermes_cli import plugins
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import parse_manifest_file
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import slash_confirm

    token = set_hermes_home_override(home)
    manager = PluginManager()
    calls = []

    class Capture:
        async def send(self, chat_id, content, metadata=None):
            calls.append((chat_id, content, metadata))
            return {"success": True}

    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True)})
    router = DeliveryRouter(config, {Platform.DISCORD: Capture()})
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="fixture-room",
        thread_id="fixture-thread",
        scope_id="fixture-guild",
        user_id="fixture-operator",
    )
    gateway = SimpleNamespace(
        delivery_router=router,
        session_store=None,
        _authorization_home_for_source=lambda s: None,
        _resolve_profile_home_for_source=lambda s: home,
        _is_user_authorized_for_source=lambda s: True,
        _session_key_for_source=lambda s: "fixture-session",
        _is_session_running=lambda key: False,
        _peek_session_state=lambda key: None,
    )
    try:
        manifest = parse_manifest_file(plugin / "plugin.yaml", plugin, "user", "")
        assert manifest is not None
        entry = manager._load_directory_module(manifest)
        entry.register(PluginContext(manifest, manager))
        package = importlib.import_module(entry.register.__module__)
        runtime = importlib.import_module(entry.register.__module__ + ".runtime").get_runtime(home)
        assert manager.has_hook("pre_gateway_dispatch")

        async def inbound(text, *, message_id, **kw):
            event = MessageEvent(text=text, source=source, message_id=message_id, **kw)
            result = await GatewayInboundHooksMixin._hm_pre_gateway_dispatch_hook(
                gateway, event, source
            )
            return event, result

        async def state_when(run_id, predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = runtime.db.get_run(run_id)
                if predicate(state):
                    return state
                await asyncio.sleep(0.03)
            raise AssertionError(state)

        with patch.object(plugins, "_delivery_manager", return_value=manager):
            opening, result = await inbound("/workflow list", message_id="opening")
            assert result is opening
            wf = {
                "id": "native-chat-review",
                "name": "Native chat review",
                "version": 1,
                "scope": {"type": "global"},
                "trigger": {"type": "manual"},
                "nodes": [
                    {"id": "start", "type": "prompt", "prompt": "Review"},
                    {"id": "gate", "type": "human_review"},
                    {"id": "done", "type": "finish"},
                ],
                "edges": [
                    {"from": "start", "to": "gate"},
                    {
                        "from": "gate",
                        "to": "gate",
                        "condition": {"type": "review_status", "equals": "needs_changes"},
                    },
                    {
                        "from": "gate",
                        "to": "done",
                        "condition": {"type": "review_status", "equals": "approved"},
                    },
                ],
            }
            runtime.specs.save({"workflow": wf}, create=True)
            status, result = package._tool(
                "workflow_run", {"workflow_id": wf["id"]}, home, task_id="fixture-session"
            )
            assert status == 200, result
            run_id = result["run_id"]
            first = await state_when(run_id, lambda r: r["status"] == "waiting")
            old_attempt = first["nodes"]["gate"]["attempt_id"]
            assert first["origin"] == "discord:fixture-room:fixture-thread"

            gateway._is_user_authorized_for_source = lambda s: False
            unauthorized, result = await inbound("approved", message_id="unauthorized")
            assert result is unauthorized
            gateway._is_user_authorized_for_source = lambda s: True

            async def native_command(choice):
                raise AssertionError("workflow stole native command confirmation")

            slash_confirm.register(
                "fixture-session", "native-control", "reload-mcp", native_command
            )
            control, result = await inbound("approved", message_id="native-control")
            assert result is control and slash_confirm.get_pending("fixture-session")
            slash_confirm.clear("fixture-session")
            assert runtime.db.get_run(run_id)["status"] == "waiting"

            first_reply, result = await inbound(
                "needs_changes revise żółć", message_id="first-reply"
            )
            assert result is None  # native gateway consumes skip directives
            second = await state_when(
                run_id,
                lambda r: (
                    r["status"] == "waiting" and r["nodes"]["gate"].get("attempt_id") != old_attempt
                ),
            )
            assert "review_note" not in second["nodes"]["gate"]
            replay = await GatewayInboundHooksMixin._hm_pre_gateway_dispatch_hook(
                gateway, first_reply, source
            )
            assert replay is None
            _, stale = await inbound(
                "approved",
                message_id="stale-quote",
                timestamp=datetime.now(),
                reply_to_message_id="old-notice",
                reply_to_text=f"[workflow-review:{old_attempt}]",
            )
            assert stale is None and runtime.db.get_run(run_id)["status"] == "waiting"
            token2 = second["nodes"]["gate"]["attempt_id"]
            _, result = await inbound(
                "scope 3",
                message_id="second-reply",
                reply_to_message_id="current-notice",
                reply_to_text=f"[workflow-review:{token2}]",
            )
            assert result is None
            await state_when(run_id, lambda r: r["status"] == "completed")
            attempts = [a["data"] for a in runtime.db.attempts(run_id) if a["node_id"] == "gate"]
            assert [(a["review_decision"], a["review_note"]) for a in attempts] == [
                ("needs_changes", "revise żółć"),
                ("approved", "scope 3"),
            ]
            confirmations = [c for c in calls if "resolved as" in c[1]]
            assert len(confirmations) == 2, calls
            assert all(
                c[0] == "fixture-room" and c[2]["thread_id"] == "fixture-thread" for c in calls
            )
            # Current native slash handlers receive session_key, not task_id.
            slash_run = json.loads(
                invoke_plugin_command(
                    partial(package._command, home=home),
                    "run " + wf["id"],
                    source=source,
                    session_id="fixture-conversation",
                    session_key="fixture-session",
                    platform="discord",
                )
            )
            slash_state = await state_when(slash_run["run_id"], lambda r: r["status"] == "waiting")
            assert slash_state["origin"] == first["origin"]
            assert (
                slash_state["notification_context"]["routing"]["session_key"] == "fixture-session"
            )
            _, result = await inbound("approved slash path", message_id="slash-approval")
            assert result is None
            await state_when(slash_run["run_id"], lambda r: r["status"] == "completed")
        manager.unload()
        assert not runtime.thread.is_alive()
        assert not manager.has_hook("pre_gateway_dispatch")
        return {
            "directory_loader_and_hook": "passed",
            "native_skip_dispatch": "passed",
            "native_slash_session_capture": "passed",
            "native_control_precedence": "passed",
            "review_loop_replay_and_stale_quote": "passed",
            "thread_confirmation_and_unload": "passed",
            "model_workers": 0,
            "external_messages": 0,
        }
    finally:
        slash_confirm.clear("fixture-session")
        manager.unload()
        reset_hermes_home_override(token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    activate(args.hermes_source)
    with tempfile.TemporaryDirectory(prefix="workflow native chat żółć ") as temporary:
        home = Path(temporary).resolve()
        os.environ.update(HERMES_HOME=str(home), HERMES_WORKFLOWS_DELIVER="")
        (home / "config.yaml").write_text("plugins:\n  isolation: in_process\n", encoding="utf-8")
        plugin = home / "plugins/hermes-workflows"
        shutil.copytree(
            Path(__file__).resolve().parents[1] / "hermes-workflows-py",
            plugin,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        print(json.dumps(asyncio.run(verify(home, plugin)), indent=2))


if __name__ == "__main__":
    main()
