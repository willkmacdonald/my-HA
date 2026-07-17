"""py_compile smoke for satellite.py (spec carry-over): openwakeword is
Pi-only so the Mac suite can't import it — but name-level breakage should
fail here, not at Phase 3 hardware bring-up."""

import py_compile
from pathlib import Path


def test_satellite_py_compiles() -> None:
    py_compile.compile(str(Path(__file__).resolve().parents[1] / "satellite.py"), doraise=True)
