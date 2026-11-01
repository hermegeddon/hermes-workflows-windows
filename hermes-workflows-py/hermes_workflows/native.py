"""Lazy, profile-scoped access to installed Hermes APIs, with explicit capabilities.

This is the adapter boundary, not a Kanban dispatcher or delivery implementation.
Probe reports API presence; it does not create boards/jobs, send messages or load
provider secrets. No process-wide environment variables are changed.
"""

from __future__ import annotations

import importlib
import inspect
from contextlib import contextmanager
from pathlib import Path

FEATURES = {
    "kanban": (
        "hermes_cli.kanban_db",
        (
            "init_db",
            "create_board",
            "list_boards",
            "create_task",
            "get_task",
            "claim_review_task",
            "write_txn",
            "kanban_db_path",
            "kanban_home",
            "board_dir",
            "archive_task",
        ),
    ),
    "kanban_store": ("hermes_cli.kanban_db_connect", ("connect", "write_txn")),
    "dispatch": ("hermes_cli.kanban_db_dispatch", ("dispatch_once",)),
    "worktrees": ("hermes_cli.worktree_ops", ("_setup_worktree", "removal_blocked")),
    "cron": ("cron.jobs", ("create_job", "list_jobs", "update_job", "remove_job")),
    "config": ("hermes_cli.config", ("load_config", "save_config")),
    "delivery": ("gateway.delivery", ("DeliveryTarget", "DeliveryRouter")),
}


class NativeUnavailable(RuntimeError):
    pass


class NativeHost:
    def __init__(self, home: Path, *, loader=importlib.import_module, context=None, gateway=None):
        self.home = Path(home).expanduser().resolve()
        self.loader = loader
        self.context = context
        self.gateway = gateway

    @contextmanager
    def scope(self):
        try:
            constants = self.loader("hermes_constants")
            set_home = constants.set_hermes_home_override
            reset_home = constants.reset_hermes_home_override
            get_home = constants.get_hermes_home
        except Exception as exc:
            raise NativeUnavailable("Hermes lacks profile-scoped home APIs") from exc
        if not all(callable(fn) for fn in (set_home, reset_home, get_home)):
            raise NativeUnavailable("Hermes lacks callable profile-scoped home APIs")
        try:
            token = set_home(self.home)
        except Exception as exc:
            raise NativeUnavailable("Hermes could not enter the requested profile scope") from exc
        try:
            try:
                selected = Path(get_home()).resolve()
            except Exception as exc:
                raise NativeUnavailable(
                    "Hermes could not resolve the selected profile home"
                ) from exc
            if selected != self.home:
                raise NativeUnavailable("Hermes did not select the requested profile home")
            yield
        finally:
            reset_home(token)

    def _operation(self, feature, operation):
        if feature not in FEATURES or operation not in FEATURES[feature][1]:
            raise NativeUnavailable(f"unsupported native operation: {feature}.{operation}")
        try:
            target = getattr(self.loader(FEATURES[feature][0]), operation)
        except Exception as exc:
            raise NativeUnavailable(f"installed Hermes lacks {feature}.{operation}") from exc
        if not callable(target):
            raise NativeUnavailable(f"installed Hermes lacks callable {feature}.{operation}")
        return target

    def module(self, feature):
        """Use inside scope() when native operations must share a connection."""
        for operation in FEATURES[feature][1]:
            self._operation(feature, operation)
        return self.loader(FEATURES[feature][0])

    def call(self, feature, operation, *args, **kwargs):
        with self.scope():
            result = self._operation(feature, operation)(*args, **kwargs)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("use NativeHost.acall for asynchronous operations")
            if inspect.isgenerator(result) or inspect.isasyncgen(result):
                raise TypeError("deferred iterators cannot leave a native profile scope")
            return result

    async def acall(self, feature, operation, *args, **kwargs):
        with self.scope():
            result = self._operation(feature, operation)(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            if inspect.isgenerator(result) or inspect.isasyncgen(result):
                raise TypeError("deferred iterators cannot leave a native profile scope")
            return result

    def probe(self):
        report = {"home": str(self.home), "scope_available": False, "features": {}}
        try:
            with self.scope():
                report["scope_available"] = True
                for feature, (module, operations) in FEATURES.items():
                    item = {"module": module, "available": False, "operations": {}, "missing": []}
                    try:
                        loaded = self.loader(module)
                        for operation in operations:
                            fn = getattr(loaded, operation, None)
                            if not callable(fn):
                                item["missing"].append(operation)
                                continue
                            try:
                                parameters = {
                                    name: {"kind": p.kind.name, "required": p.default is p.empty}
                                    for name, p in inspect.signature(fn).parameters.items()
                                }
                            except (ValueError, TypeError):
                                parameters = None
                            item["operations"][operation] = {"parameters": parameters}
                        item["available"] = not item["missing"]
                    except Exception as exc:
                        # Error type only: exception text can contain host config.
                        item["reason"] = f"import failed ({type(exc).__name__})"
                    report["features"][feature] = item
        except NativeUnavailable as exc:
            report["scope_error"] = str(exc)
        report["hooks_available"] = callable(getattr(self.context, "register_hook", None))
        report["delivery_router_available"] = callable(
            getattr(getattr(self.gateway, "delivery_router", None), "deliver", None)
        )
        return report
