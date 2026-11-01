"""Resolve the backend belonging to this installation, without changing sys.path."""

import _imp
import hashlib
import importlib.util
import os
import sys
from pathlib import Path


def load_backend(root):
    root = Path(root).resolve()
    entry = root / "hermes_workflows" / "__init__.py"

    def matches(module):
        filename = getattr(module, "__file__", None)
        return (
            filename
            and Path(filename).resolve() == entry
            and callable(getattr(module, "register", None))
        )

    # Hermes evicts its directory namespace on reload. Prefer that namespace
    # over canonical imports made by another profile, a CLI or a worker.
    _imp.acquire_lock()
    try:
        for name, module in tuple(sys.modules.items()):
            if (
                name.startswith("hermes_plugins.")
                and name.endswith(".hermes_workflows")
                and matches(module)
            ):
                return module
        canonical = sys.modules.get("hermes_workflows")
        if canonical is not None and matches(canonical):
            return canonical  # Existing same-installation CLI/import users.
        identity = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:24]
        name = "_hermes_workflows_backend_" + identity
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                name, entry, submodule_search_locations=[str(entry.parent)]
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load workflow backend at {root}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                for loaded in tuple(sys.modules):
                    if loaded == name or loaded.startswith(name + "."):
                        sys.modules.pop(loaded, None)
                raise
        return sys.modules[name]
    finally:
        _imp.release_lock()
