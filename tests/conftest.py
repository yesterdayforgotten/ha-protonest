"""Test setup.

``pynestproto`` is imported as a top-level package (not through
``custom_components.protonest``) so tests don't need Home Assistant installed.

Fixtures are real captures from the author's account and contain PII, so they
are gitignored. Point PROTONEST_FIXTURES at a directory holding them; tests that
need a missing fixture are skipped.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "protonest"))

from pynestproto import codec  # noqa: E402

FIXTURE_DIR = Path(os.environ.get("PROTONEST_FIXTURES", ROOT / "old" / "sdk-deobfuscated"))
# Live recordings made with tools/observe_cli.py --record (also gitignored).
CAPTURE_DIR = ROOT / "captures"


@pytest.fixture(scope="session", autouse=True)
def _trait_types() -> None:
    codec.load_trait_types()


@pytest.fixture(scope="session")
def capture():
    """Return a loader for a captured fixture file's bytes."""

    def load(name: str) -> bytes:
        for directory in (FIXTURE_DIR, CAPTURE_DIR):
            path = directory / name
            if path.exists():
                return path.read_bytes()
        pytest.skip(f"fixture not available: {name}")

    return load
