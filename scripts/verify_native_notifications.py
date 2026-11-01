"""Exercise installed Hermes delivery/subscriptions using capture-only adapters.

No messaging account, provider, live gateway or existing profile is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_native_host import activate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args()
    activate(args.hermes_source)
    with tempfile.TemporaryDirectory(prefix="workflow delivery żółć ") as temporary:
        home = Path(temporary).resolve()
        os.environ.update(
            HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), HERMES_WORKFLOWS_DELIVER=""
        )
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
            os.environ.pop(key, None)
        (home / "config.yaml").write_text("model: test-only\n", encoding="utf-8")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
        from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
        from gateway.delivery import DeliveryRouter
        from gateway.session import SessionSource
        from hermes_cli import kanban_db_connect as store
        from hermes_cli import kanban_db_notify as notify
        from hermes_workflows.db import Database
        from hermes_workflows.executor import Executor, Result
        from hermes_workflows.kanban import KanbanExecutor
        from hermes_workflows.native import NativeHost
        from hermes_workflows.runtime import Runtime
        from hermes_workflows.specs import SpecStore

        calls = []

        class CaptureAdapter:
            async def send(self, chat_id, content, metadata=None):
                calls.append({"chat": chat_id, "text": content, "metadata": metadata})
                return {"success": True}

        async def verify():
            config = GatewayConfig(
                platforms={
                    Platform.DISCORD: PlatformConfig(
                        enabled=True,
                        home_channel=HomeChannel(Platform.DISCORD, "fixture-home", "Fixture home"),
                    )
                }
            )
            router = DeliveryRouter(config, {Platform.DISCORD: CaptureAdapter()})
            source = SessionSource(
                platform=Platform.DISCORD, chat_id="fixture-origin", thread_id="fixture-thread"
            )
            gateway = SimpleNamespace(
                delivery_router=router,
                _session_key_for_source=lambda s: "fixture-key",
                _authorization_home_for_source=lambda s: None,
            )
            db = Database(home / "workflows/runs.db")

            async def worker(node, run):
                return Result(output="Captured native result żółć")

            runtime = Runtime(
                db, SpecStore([home / "workflows/global"]), Executor(home=home, agent_runner=worker)
            )
            try:
                await runtime.origins.capture(SimpleNamespace(source=source), gateway)
                assert (
                    runtime.origins.origin_for("fixture-key")
                    == "discord:fixture-origin:fixture-thread"
                )
                runtime.start()
                workflow = {
                    "id": "delivery-native",
                    "name": "Capture only",
                    "version": 1,
                    "scope": {"type": "global"},
                    "trigger": {"type": "manual"},
                    "deliver": "origin",
                    "nodes": [
                        {
                            "id": "work",
                            "type": "agent_task",
                            "profile": "default",
                            "prompt": "test",
                        },
                        {"id": "done", "type": "finish"},
                    ],
                    "edges": [{"from": "work", "to": "done"}],
                }
                run = runtime.engine.start(
                    workflow,
                    origin=runtime.origins.origin_for("fixture-key"),
                    origin_metadata=runtime.origins.routing_for("fixture-key"),
                )
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    records = runtime.notifications.records(run["run_id"])
                    if records and records[0]["status"] in ("sent", "unknown"):
                        break
                    await asyncio.sleep(0.02)
                assert db.get_run(run["run_id"])["status"] == "completed"
                assert records[0]["status"] == "sent", records
                assert (
                    calls[0]["chat"] == "fixture-origin"
                    and calls[0]["metadata"]["thread_id"] == "fixture-thread"
                )
                assert calls[0]["text"] == "Captured native result żółć"
                sender = runtime.notifications.sender
                assert await sender.send("discord", "Home-channel capture", "home-proof")
                assert calls[-1]["chat"] == "fixture-home"
                assert not await sender.send("unknown-platform:fixture", "No fallback", "bad-proof")

                host = NativeHost(home)
                adapter = KanbanExecutor(db, host)
                board_wf = {
                    **workflow,
                    "id": "subscription-native",
                    "scope": {"type": "project", "projects": ["notification-proof"]},
                }
                runtime.close()
                state = runtime.engine.start(
                    board_wf, origin="discord:fixture-origin:fixture-thread"
                )
                # Hold a separate driver lease to exercise the real subscription
                # helper without letting the gateway claim a model worker.
                # Do not start an agent: this verifier owns all native card work.
                assert db.claim(state["run_id"], "proof")

                def begin(r):
                    r["status"] = "running"
                    r["nodes"]["work"].update(
                        status="running",
                        executor="kanban",
                        started_at=time.time(),
                        executor_home=str(home),
                    )

                state = db.mutate_run(state["run_id"], begin, owner="proof")
                node = {**board_wf["nodes"][0], "profile": "default"}
                for _ in range(2):
                    handle = adapter.schedule(
                        node,
                        state,
                        "notification-proof",
                        owner="proof",
                        revision=0,
                        attempt_id=state["nodes"]["work"]["attempt_id"],
                        timeout=60,
                    )
                with host.scope():
                    conn = store.connect(board="notification-proof")
                    try:
                        subs = [
                            dict(r)
                            for r in conn.execute(
                                "SELECT * FROM kanban_notify_subs WHERE task_id=?",
                                (handle["task_id"],),
                            )
                        ]
                        assert len(subs) == 1 and subs[0]["notifier_profile"] == "default", {
                            "subs": subs,
                            "node": db.get_run(state["run_id"])["nodes"]["work"],
                        }
                        assert subs[0]["thread_id"] == "fixture-thread"
                        # Native helper initializes at the current event cursor;
                        # resubscribe leaves it intact, rather than replaying history.
                        cursor = subs[0]["last_event_id"]
                        notify.add_notify_sub(
                            conn,
                            task_id=handle["task_id"],
                            platform="discord",
                            chat_id="fixture-origin",
                            thread_id="fixture-thread",
                            notifier_profile="default",
                        )
                        assert (
                            conn.execute("SELECT last_event_id FROM kanban_notify_subs").fetchone()[
                                0
                            ]
                            == cursor
                        )
                    finally:
                        conn.close()
                print(
                    __import__("json").dumps(
                        {
                            "native_router_captured": len(calls),
                            "native_subscriptions": len(subs),
                            "external_sends": 0,
                            "model_workers": 0,
                            "home_channel": True,
                            "thread_origin": True,
                        }
                    )
                )
            finally:
                runtime.close()

        asyncio.run(verify())


if __name__ == "__main__":
    main()
