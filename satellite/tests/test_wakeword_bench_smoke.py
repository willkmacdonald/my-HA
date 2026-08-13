"""py_compile smoke for wakeword_bench.py: openwakeword is installed only on
the Pi (requirements.txt, not requirements-mac.txt), so the Mac suite can't
import the module — but name-level breakage (e.g. a renamed channelpick export)
should fail here, not at Phase 3 hardware bring-up. Mirrors test_satellite_smoke.
"""

import py_compile
from pathlib import Path


def test_wakeword_bench_py_compiles() -> None:
    py_compile.compile(str(Path(__file__).resolve().parents[1] / "wakeword_bench.py"), doraise=True)
