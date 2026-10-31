"""File-based Hermes plugin entrypoint (also works when loaded by file path)."""

import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent)
if _root not in sys.path:
    sys.path.insert(0, _root)
from hermes_workflows import register  # noqa: E402,F401
