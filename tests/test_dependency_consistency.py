"""Keep Home Assistant runtime requirements reproducible."""

from __future__ import annotations

import json
from pathlib import Path


def test_runtime_requirements_match_manifest() -> None:
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "custom_components/loxone/manifest.json").read_text())
    requirements = [
        line.strip()
        for line in (root / "requirements-runtime.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert requirements == manifest["requirements"]
