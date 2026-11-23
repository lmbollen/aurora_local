"""Run the offline test scripts in analysis/ under pytest.

The scripts need no heat pump and no Home Assistant: they use fake modules
on 127.0.0.x. Replays of a real raw archive run only when AURORA_ARCHIVE
points at one (the archive is personal data and not in the repository).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "custom_components" / "aurora_local" / "pyauroralocal"
ANALYSIS = ROOT / "analysis"
ARCHIVE = os.environ.get("AURORA_ARCHIVE")


def _run(script: str, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(ANALYSIS / script), str(LIB), *args],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


@pytest.mark.parametrize("script", ["test_fixes.py", "test_reconnect.py", "test_rediscover.py"])
def test_offline(script: str) -> None:
    _run(script)


@pytest.mark.skipif(not ARCHIVE, reason="set AURORA_ARCHIVE to a raw archive folder")
@pytest.mark.parametrize("script", ["test_history.py", "test_bootstrap.py", "replay_calibration.py",
                                    "replay_events.py", "replay_model.py", "check_consistency.py"])
def test_replay(script: str) -> None:
    print(_run(script, ARCHIVE))


def test_library_is_standalone() -> None:
    """The library imports only the standard library and itself."""
    stdlib = sys.stdlib_module_names
    for path in LIB.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative: inside the package
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                assert top in stdlib or top == "__future__", f"{path.name} imports {name}"
