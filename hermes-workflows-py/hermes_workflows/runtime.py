"""One asyncio loop per process, independent of HTTP request lifetimes."""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import settings
from .db import ConflictError, Database, hermes_home
from .engine import Engine
from .executor import Executor
from .gate_reply import ChatReviews
from .native import NativeHost
from .native_schedules import Schedules
from .notifications import Notifications
from .origin import NativeSender, Origins
from .schedules import Cron, iso_now
from .specs import SpecStore
from .telemetry import TelemetryStore

log = logging.getLogger(__name__)


class Runtime:
    def __init__(
        self,
        db: Database | None = None,
        specs: SpecStore | None = None,
        executor: Executor | None = None,
    ):
        self.db = db or Database()
        self.specs = specs or SpecStore()
        self.engine = Engine(self.db, executor)
        self.home = self.db.path.parent.parent.resolve()
        self.memory = self.engine.memory_policy
        self.engine.executor.home = self.engine.executor.home or self.home
        self.origins = Origins(NativeHost(self.home))
        self.notifications = Notifications(self.db, self.home, NativeSender(self.origins))
        self.chat_reviews = ChatReviews(self)
        self.telemetry = TelemetryStore(self.db.path)
        self.schedules = Schedules(self)
        self._schedule_mode = None
        self.owner = uuid.uuid4().hex
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._startup_error: BaseException | None = None
        self._closed = False

    @property
    def closed(self):
        return self._closed

    async def pre_dispatch(self, event=None, gateway=None, session_store=None, **kwargs):
        await self.origins.capture(event, gateway, session_store, **kwargs)
        return await self.chat_reviews.route(event, gateway, session_store)

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("workflow runtime is closed")
            if self.thread is None:
                self.thread = threading.Thread(
                    target=contextvars.copy_context().run,
                    args=(self._thread_main,),
                    name="hermes-workflows",
                    daemon=True,
                )
                self.thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("workflow runtime did not start")
        if self._startup_error:
            raise RuntimeError("workflow runtime failed") from self._startup_error
        return self

    def _thread_main(self):
        try:
            with asyncio.Runner() as runner:
                self.loop = runner.get_loop()
                runner.run(self._supervise())
        except BaseException as exc:
            self._startup_error = exc
            log.exception("workflow runtime stopped")
            self._ready.set()

    async def _supervise(self):
        self._stop = asyncio.Event()
        # Every host (gateway, CLI and HTTP) gets the same startup reconciliation.
        # No tool call or HTTP request is needed before schedules become active.
        for detail in self.specs.all():
            try:
                self.sync_schedule(detail["workflow"])
            except (KeyError, ValueError, RuntimeError):
                log.warning("invalid workflow schedule: %s", detail["path"])
        self._ready.set()
        tasks = {}
        delivery_task = None
        memory_task = None
        last_schedules = 0.0
        try:
            while not self._stop.is_set():
                for run_id, task in list(tasks.items()):
                    if task.done():
                        try:
                            task.result()
                        except ConflictError:
                            log.debug("workflow driver was superseded: %s", run_id)
                        except Exception:
                            log.exception("workflow driver failed: %s", run_id)
                        del tasks[run_id]
                for run_id in self.db.active_ids():
                    if run_id in tasks:
                        continue
                    state = self.db.get_run(run_id)
                    if state["status"] == "waiting":
                        continue
                    # A process can drive several successive revisions. A new
                    # token prevents an old driver's finally/release from
                    # clearing a replacement driver's lease in the same process.
                    owner = f"{self.owner}:{uuid.uuid4().hex}"
                    if self.db.claim(run_id, owner):
                        tasks[run_id] = asyncio.create_task(
                            self.engine.drive(run_id, owner, checkpoint=True)
                        )
                if time.monotonic() - last_schedules >= 1:
                    try:
                        await asyncio.to_thread(self.tick_schedules)
                        await asyncio.to_thread(self.engine.reconcile_external)
                    except Exception:
                        log.exception("schedule tick failed")
                    last_schedules = time.monotonic()
                    if delivery_task is None or delivery_task.done():
                        if delivery_task is not None:
                            try:
                                delivery_task.result()
                            except Exception:
                                log.exception("notification dispatch failed")
                        delivery_task = asyncio.create_task(self.notifications.dispatch())
                    if memory_task is None or memory_task.done():
                        if memory_task is not None:
                            try:
                                memory_task.result()
                            except Exception as exc:
                                log.warning("memory dispatch unavailable (%s)", type(exc).__name__)
                        memory_task = asyncio.create_task(self.memory.dispatch())
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.1)
                except TimeoutError:
                    pass
        finally:
            if memory_task:
                memory_task.cancel()
                await asyncio.gather(memory_task, return_exceptions=True)
            if delivery_task:
                delivery_task.cancel()
                await asyncio.gather(delivery_task, return_exceptions=True)
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    def close(self):
        self.origins.clear()
        with self._lock:
            self._closed = True
        # A concurrent registration may still be starting its event loop.
        if self.thread and not self._ready.is_set():
            self._ready.wait(10)
        if self.loop and self._stop:
            try:
                self.loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass  # Already stopped; repeated unload/atexit is harmless.
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                log.warning("workflow runtime is still finishing shutdown")

    def sync_schedule(self, workflow: dict):
        return self.schedules.sync(workflow)

    def tick_schedules(self):
        mode = settings.values(self.db)["scheduler_backend"]
        if mode != self._schedule_mode:
            for detail in self.specs.all():
                self.sync_schedule(detail["workflow"])
            self._schedule_mode = mode
        # An atomic due-time update prevents gateway and dashboard processes
        # from firing the same schedule. Single-flight run creation is a second
        # guard. Missed intervals coalesce into one fire after restart.
        for row in self.db.schedules():
            if (
                row.get("authority") == "native"
                or not row["enabled"]
                or row.get("deleted")
                or not row.get("next_run")
            ):
                continue
            now = datetime.now(timezone.utc)
            if datetime.fromisoformat(row["next_run"]) > now:
                continue
            claimed = False
            with self.db.connect(write=True) as con:
                current = con.execute(
                    "SELECT data FROM py_schedules WHERE id=?", (row["hermes_cron_id"],)
                ).fetchone()
                if current:
                    live = json.loads(current[0])
                    if (
                        live.get("enabled")
                        and live.get("authority") != "native"
                        and not live.get("deleted")
                        and live.get("next_run") == row["next_run"]
                    ):
                        live["next_run"] = (
                            Cron(live["cron_expression"], live["timezone"]).next(now).isoformat()
                        )
                        live["last_run"] = iso_now()
                        con.execute(
                            "UPDATE py_schedules SET data=? WHERE id=?",
                            (json.dumps(live), row["hermes_cron_id"]),
                        )
                        claimed = True
            if claimed:
                try:
                    detail = self.specs.get(row["workflow_id"])
                    self.engine.start(detail["workflow"], workflow_path=detail["path"], scheduled=True)
                except ConflictError:
                    log.info("scheduled run skipped while disabled or busy: %s", row["workflow_id"])
                except Exception:
                    log.exception("scheduled workflow failed: %s", row["workflow_id"])


_runtimes: dict[str, Runtime] = {}
_runtime_owners: dict[Runtime, int] = {}
_runtime_lock = threading.RLock()


def _home_key(home: Path) -> str:
    return os.path.normcase(str(home.expanduser().resolve()))


def get_runtime(home: Path | None = None) -> Runtime:
    home = (Path(home) if home is not None else hermes_home()).expanduser().resolve()
    key = _home_key(home)
    with _runtime_lock:
        runtime = _runtimes.get(key)
        if runtime is None or runtime.closed:
            root = home / "workflows"
            runtime = Runtime(
                Database(root / "runs.db"), SpecStore([root / "global", root / "templates"])
            )
            runtime.engine.executor.home = home
            _runtimes[key] = runtime
            atexit.register(runtime.close)
        try:
            return runtime.start()
        except BaseException:
            _runtimes.pop(key, None)
            runtime.close()
            raise


def stop_runtime(runtime: Runtime):
    """Dispose only the captured generation, never a replacement for that home."""
    with _runtime_lock:
        for key, current in list(_runtimes.items()):
            if current is runtime:
                del _runtimes[key]
        _runtime_owners.pop(runtime, None)
        atexit.unregister(runtime.close)
    runtime.close()


def own_runtime(runtime: Runtime):
    """Share lifetime between plugin registration and an embedded HTTP host."""
    released = False
    with _runtime_lock:
        _runtime_owners[runtime] = _runtime_owners.get(runtime, 0) + 1

    def release():
        nonlocal released
        with _runtime_lock:
            if released:
                return
            released = True
            remaining = _runtime_owners.get(runtime, 1) - 1
            if remaining > 0:
                _runtime_owners[runtime] = remaining
                return
            _runtime_owners.pop(runtime, None)
            for key, current in list(_runtimes.items()):
                if current is runtime:
                    del _runtimes[key]
            atexit.unregister(runtime.close)
        runtime.close()

    return release
