#!/usr/bin/env python3
"""Stage 1 (data): validate flat ``data/``; on failure fetch WRDS overwrite.

Flow::

    validate_local (four CSVs only) → (pass) stop
                                    → (fail) fetch_wrds → overwrite four CSVs
                                      + coverage.json fetch snapshot

Canonical WRDS path writes::

    weights.csv, daily_prices.csv, calendar.csv, events.csv
    (+ coverage.json provenance; not required for later cache hits)

Force refresh: delete or corrupt ``data/`` then re-run. Edit switches in
``config.py``. Next: ``python scripts/run_2_fund.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
)


def main() -> int:
    load_project_dotenv()
    ensure_data_dirs()
    cfg = PipelineConfig.from_user_config(config)
    info = prepare_data(cfg)
    print(f"source={info.get('source')} universe={info.get('universe')}", flush=True)

    for i, (name, title) in enumerate(CORE_TABLES):
        print_csv(DATA_DIR / name, title=title, blank_before=i > 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
