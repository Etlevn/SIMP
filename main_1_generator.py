#!/usr/bin/env python3
"""Phase 1: synthetic multi-MIC order generator (stub).

Writes ``output/1_generator/STATUS.txt`` until the generator is implemented.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paths import OUTPUT_1_GENERATOR, ensure_phase_output_dir

STATUS = """Phase 1 — synthetic order generator: not implemented yet.

Expected later: multi-MIC synthetic PM/Trader/(child)/fill chains under this
directory, priced near same-day VWAP or a documented synthetic stand-in.
"""


def main() -> int:
    ensure_phase_output_dir(OUTPUT_1_GENERATOR)
    (OUTPUT_1_GENERATOR / "STATUS.txt").write_text(STATUS)
    print("phase=1_generator status=stub", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
