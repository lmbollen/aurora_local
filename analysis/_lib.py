"""Import the pyauroralocal library for the analysis scripts.

If it is installed (``uv sync`` in the repository root) that copy is used.
Otherwise it is loaded from a path: the integration folder
(``custom_components/aurora_local``) or the library folder itself. The
integration folder can't simply go on ``sys.path``: its ``select.py`` would
shadow the standard library module of the same name.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SUBMODULES = ("framing", "state", "discovery", "client", "fields", "calibration",
              "monitoring", "processor", "archive", "history")


def load(path: str | None = None) -> ModuleType:
    if path is None:
        lib = importlib.import_module("pyauroralocal")
    else:
        root = Path(path)
        if (root / "pyauroralocal").is_dir():
            root = root / "pyauroralocal"
        spec = importlib.util.spec_from_file_location(
            "pyauroralocal", root / "__init__.py", submodule_search_locations=[str(root)]
        )
        lib = importlib.util.module_from_spec(spec)
        sys.modules["pyauroralocal"] = lib
        spec.loader.exec_module(lib)
    for name in SUBMODULES:
        importlib.import_module(f"pyauroralocal.{name}")
    return lib
