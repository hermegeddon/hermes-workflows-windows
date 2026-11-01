from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-workflows-py"


def load_file(name, path, *, package=False):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow import copies ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.loaded_before = set(sys.modules)
        self.addCleanup(self.clear_modules)

    def clear_modules(self):
        for name in tuple(sys.modules):
            if name not in self.loaded_before and (
                name.startswith("workflow_test_")
                or name.startswith("_hermes_workflows_backend_")
                or name.startswith("hermes_plugins.workflow_test_")
            ):
                sys.modules.pop(name, None)

    def copy(self, name):
        target = self.root / name
        shutil.copytree(
            PLUGIN,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "dashboard", "desktop"),
        )
        return target

    def poison(self):
        old = types.ModuleType("hermes_workflows")
        old.__file__ = str(self.root / "older-profile/hermes_workflows/__init__.py")

        def forbidden(*args):
            raise AssertionError("another profile's backend was selected")

        old.register = forbidden
        return old

    def test_native_directory_entry_ignores_poisoned_canonical_package(self):
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        with patch.dict(sys.modules, {"hermes_plugins": parent, "hermes_workflows": self.poison()}):
            entry = load_file(
                "hermes_plugins.workflow_test_default", PLUGIN / "__init__.py", package=True
            )
            self.assertEqual(
                entry.register.__module__, "hermes_plugins.workflow_test_default.hermes_workflows"
            )
            backend = sys.modules[entry.register.__module__]
            helper = load_file("workflow_test_loader", PLUGIN / "_bootstrap.py")
            self.assertIs(helper.load_backend(PLUGIN), backend)

    def test_distinct_installations_and_concurrent_file_loads_stay_isolated(self):
        one, two = self.copy("default"), self.copy("qa")
        helper = load_file("workflow_test_loader", PLUGIN / "_bootstrap.py")
        with patch.dict(sys.modules, {"hermes_workflows": self.poison()}):
            with ThreadPoolExecutor(max_workers=4) as pool:
                loaded = list(pool.map(helper.load_backend, [one, two, one, two]))
            self.assertIs(loaded[0], loaded[2])
            self.assertIs(loaded[1], loaded[3])
            self.assertIsNot(loaded[0], loaded[1])
            # Hosted Windows TEMP may contain RUNNER~1 while the loader resolves
            # its long name. Assert filesystem ownership, not path spelling.
            self.assertTrue(Path(loaded[0].__file__).parent.samefile(one / "hermes_workflows"))
            self.assertTrue(Path(loaded[1].__file__).parent.samefile(two / "hermes_workflows"))

    def test_failed_file_import_does_not_leave_a_partial_backend(self):
        root = self.copy("broken")
        (root / "hermes_workflows/__init__.py").write_text(
            "raise RuntimeError('fixture import failed')\n", encoding="utf-8"
        )
        helper = load_file("workflow_test_loader", PLUGIN / "_bootstrap.py")
        with self.assertRaisesRegex(RuntimeError, "fixture import failed"):
            helper.load_backend(root)
        (root / "hermes_workflows/__init__.py").write_text(
            "def register(ctx): return 'recovered'\n", encoding="utf-8"
        )
        self.assertEqual(helper.load_backend(root).register(None), "recovered")

    def test_dashboard_uses_the_matching_directory_backend_not_global_imports(self):
        if importlib.util.find_spec("fastapi") is None:
            self.skipTest("optional host dependency")
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        with patch.dict(sys.modules, {"hermes_plugins": parent, "hermes_workflows": self.poison()}):
            entry = load_file(
                "hermes_plugins.workflow_test_dashboard", PLUGIN / "__init__.py", package=True
            )
            api = load_file("workflow_test_dashboard_api", PLUGIN / "dashboard/plugin_api.py")
            self.assertIs(api._backend, sys.modules[entry.register.__module__])
            self.assertTrue(api.router.routes)
