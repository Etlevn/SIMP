#!/usr/bin/env python3
"""Independent data tool: validate flat ``data/``; on failure fetch WRDS overwrite.

Not a product phase. Writes only under ``data/`` (four CSVs + benchmark + optional
coverage). Phase 2 fund replay is ``main_2_fund.py``.

Flow::

    validate_local (four CSVs only) → (pass) continue
                                    → (fail) fetch_wrds → overwrite four CSVs
                                      + coverage.json fetch snapshot
    ensure_benchmark → reuse or fetch ``benchmark_sp500_tr.csv``

Force refresh: delete or corrupt ``data/`` then re-run. Edit switches in
``config.py``.
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

import config
from paths import DATA_DIR, ensure_data_dirs
from pipeline import PipelineConfig, prepare_data
from preview import print_csv
from wrds_auth import load_project_dotenv

CORE_TABLES = (
    ("weights.csv", "weights"),
    ("daily_prices.csv", "prices"),
    ("calendar.csv", "calendar"),
    ("events.csv", "events"),
    ("benchmark_sp500_tr.csv", "benchmark"),
)


def main() -> int:
    load_project_dotenv()
    ensure_data_dirs()
    cfg = PipelineConfig.from_user_config(config)
    info = prepare_data(cfg)
    bench = info.get("benchmark") or {}
    print(
        f"source={info.get('source')} universe={info.get('universe')} "
        f"benchmark={bench.get('source')} rows={bench.get('rows')}",
        flush=True,
    )

    for i, (name, title) in enumerate(CORE_TABLES):
        print_csv(DATA_DIR / name, title=title, blank_before=i > 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
