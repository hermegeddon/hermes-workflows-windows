"""Standard Hermes dashboard plugin route module."""

import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parents[1])
if _root not in sys.path:
    sys.path.insert(0, _root)
from hermes_workflows.api import create_router

router = create_router()
