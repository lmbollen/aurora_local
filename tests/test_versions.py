"""The integration, the library and the package metadata share one version."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "aurora_local"


def test_versions_match() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))["version"]
    init = (INTEGRATION / "pyauroralocal" / "__init__.py").read_text(encoding="utf-8")
    library = re.search(r'__version__ = "([^"]+)"', init).group(1)
    assert manifest == library


def test_translations_match_strings() -> None:
    strings = (INTEGRATION / "strings.json").read_text(encoding="utf-8")
    english = (INTEGRATION / "translations" / "en.json").read_text(encoding="utf-8")
    assert json.loads(strings) == json.loads(english)
