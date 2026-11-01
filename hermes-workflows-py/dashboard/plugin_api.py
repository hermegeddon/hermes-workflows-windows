"""Standard Hermes dashboard plugin route module."""

import importlib
import importlib.util
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_workflow_bootstrap", _root / "_bootstrap.py")
_loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loader)
_backend = _loader.load_backend(_root)
create_router = importlib.import_module(_backend.__name__ + ".api").create_router

router = create_router()
