"""Process creation must not stop the workflow event loop or lose late children."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-workflows-py"))
from hermes_workflows.executor import run_process
from hermes_workflows.process_launch import ProcessLaunch


class ProcessLifecycleTests(unittest.TestCase):
    def test_peer_workflow_is_dispatched_while_another_launch_is_blocked(self):
        from hermes_workflows.db import Database
        from hermes_workflows.runtime import Runtime
        from hermes_workflows.specs import SpecStore

        entered, release, peer_started = (threading.Event() for _ in range(3))
        original_popen = subprocess.Popen

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test launch release missing")
            return original_popen(*args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="concurrent workflow launch żółć ") as directory:
            home = Path(directory)
            db = Database(home / "workflows/runs.db")
            db.save_settings({"scripts_enabled": True})
            runtime = Runtime(db, SpecStore([home / "specs"]))
            original_drive = runtime.engine.drive

            async def drive(run_id, *args, **kwargs):
                if run_id == "peer":
                    peer_started.set()
                return await original_drive(run_id, *args, **kwargs)

            common = {
                "name": "Launch QA",
                "version": 1,
                "scope": {"type": "global"},
                "trigger": {"type": "manual"},
                "edges": [],
            }
            try:
                with (
                    patch("subprocess.Popen", delayed),
                    patch.object(runtime.engine, "drive", drive),
                ):
                    runtime.start()
                    runtime.engine.start(
                        {
                            **common,
                            "id": "slow",
                            "nodes": [
                                {
                                    "id": "script",
                                    "type": "script",
                                    "command": "echo ready",
                                    "timeout_seconds": 20,
                                }
                            ],
                        }
                    )
                    self.assertTrue(entered.wait(10))
                    runtime.engine.start(
                        {**common, "id": "peer", "nodes": [{"id": "done", "type": "finish"}]},
                        run_id="peer",
                    )
                    self.assertTrue(
                        peer_started.wait(5), "another run was starved by process creation"
                    )
            finally:
                release.set()
                runtime.close()

    def test_cleanup_does_not_queue_behind_a_saturated_executor(self):
        original = ProcessLaunch.start
        pool_release = threading.Event()

        async def exercise():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
            started, busy = asyncio.Event(), asyncio.Event()

            def launch(state, *args, **kwargs):
                original(state, *args, **kwargs)
                loop.call_soon_threadsafe(started.set)

            def occupy_pool():
                loop.call_soon_threadsafe(busy.set)
                pool_release.wait(10)

            task = occupancy = None
            try:
                with patch.object(ProcessLaunch, "start", launch):
                    task = asyncio.create_task(
                        run_process([sys.executable, "-c", "import time; time.sleep(10)"])
                    )
                    await asyncio.wait_for(started.wait(), 10)
                    occupancy = asyncio.create_task(asyncio.to_thread(occupy_pool))
                    await asyncio.wait_for(busy.wait(), 10)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 3)
            finally:
                pool_release.set()
                await asyncio.gather(
                    *(t for t in (task, occupancy) if t is not None), return_exceptions=True
                )

        asyncio.run(exercise())

    def late_creation(self, *, timeout):
        entered, release, created = (threading.Event() for _ in range(3))
        processes = []
        original = subprocess.Popen
        with tempfile.TemporaryDirectory(prefix="late worker żółć ") as directory:
            marker = Path(directory) / "unexpected child execution.txt"

            def delayed(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("test launch release missing")
                process = original(*args, **kwargs)
                processes.append(process)
                created.set()
                return process

            async def exercise():
                program = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran',encoding='utf-8')"
                task = asyncio.create_task(
                    run_process([sys.executable, "-c", program, str(marker)], timeout=timeout or 15)
                )
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                    if timeout:
                        with self.assertRaises(TimeoutError):
                            await task
                    else:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    # Cancellation/timeout completes while creation is still gated.
                    self.assertFalse(created.is_set())
                    release.set()
                    self.assertTrue(await asyncio.to_thread(created.wait, 10))
                    await asyncio.to_thread(processes[0].wait, 10)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)

            try:
                with patch("subprocess.Popen", delayed):
                    asyncio.run(exercise())
            finally:
                release.set()
            self.assertIsNotNone(
                processes[0].poll(), "late-created process survived its cancelled owner"
            )
            if os.name == "nt":
                self.assertFalse(marker.exists(), "an abandoned Windows child was resumed")

    def test_cancellation_reaps_a_process_created_after_the_awaiter_left(self):
        self.late_creation(timeout=None)

    def test_deadline_reaps_a_process_created_after_the_awaiter_left(self):
        self.late_creation(timeout=2)

    @unittest.skipUnless(os.name == "nt", "suspended Windows process ownership")
    def test_job_setup_failure_reaps_the_suspended_process(self):
        original = subprocess.Popen
        processes = []

        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("subprocess.Popen", capture),
            patch(
                "hermes_workflows.executor.WindowsJob", side_effect=OSError("synthetic job refusal")
            ),
        ):
            with self.assertRaisesRegex(OSError, "synthetic job refusal"):
                asyncio.run(run_process([sys.executable, "-c", "print('unreachable')"]))
        self.assertIsNotNone(processes[0].poll())

    def test_event_loop_keeps_advancing_during_slow_process_creation(self):
        entered, release, heartbeat = (threading.Event() for _ in range(3))
        observed = []
        original = subprocess.Popen

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test launch release missing")
            return original(*args, **kwargs)

        def observer():
            if entered.wait(10):
                observed.append(heartbeat.wait(3))
            release.set()

        watcher = threading.Thread(target=observer, daemon=True)
        watcher.start()

        async def exercise():
            task = asyncio.create_task(
                run_process([sys.executable, "-c", "print('ready')"], timeout=15)
            )
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                heartbeat.set()
                return await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

        try:
            with patch("subprocess.Popen", delayed):
                result = asyncio.run(exercise())
        finally:
            release.set()
            watcher.join(10)
        self.assertEqual(result.outcome, "success")
        self.assertIn("ready", result.output)
        self.assertEqual(observed, [True], "a slow Popen blocked the shared event loop")


if __name__ == "__main__":
    unittest.main()
