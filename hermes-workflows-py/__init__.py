"""File-based Hermes plugin entrypoint (also works when loaded by file path)."""

import importlib.util
import sys
from pathlib import Path

__all__ = ["register"]

if __package__ and __package__ in sys.modules:
    # Hermes gives every profile directory its own package namespace.
    from .hermes_workflows import register
else:
    # Also support hosts/tests that execute this file without a package.
    _root = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location("_workflow_bootstrap", _root / "_bootstrap.py")
    _loader = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_loader)
    register = _loader.load_backend(_root).register
