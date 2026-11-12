"""Project path conventions: flat ``data/`` inputs + ``output/2_fund/`` fund outputs."""
from __future__ import annotations

import shutil
from pathlib import Path

# src/paths.py -> repo root is one level up
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output" / "2_fund"
CONFIG_DIR = PROJECT_ROOT / "configs"

WEIGHTS_CSV = DATA_DIR / "weights.csv"
PRICES_CSV = DATA_DIR / "daily_prices.csv"
CALENDAR_CSV = DATA_DIR / "calendar.csv"
EVENTS_CSV = DATA_DIR / "events.csv"
COVERAGE_JSON = DATA_DIR / "coverage.json"

CORE_INPUT_FILES = (
    "weights.csv",
    "daily_prices.csv",
    "calendar.csv",
    "events.csv",
)

# Slim schemas written to data/ (readers still tolerate extra columns).
WEIGHTS_COLUMNS = ("date", "reference_date", "permno", "cap_weight_proxy")
PRICES_COLUMNS = ("permno", "date", "close_price", "price_flag")
CALENDAR_COLUMNS = ("date",)
EVENTS_COLUMNS = (
    "source_event_id",
    "permno",
    "event_type",
    "ex_date",
    "pay_date",
    "split_ratio",
    "cash_per_share",
    "currency",
)

# Stage 2 fund disk artifacts under output/2_fund/
FUND_OUTPUT_TABLES = ("daily_nav", "daily_holdings", "fills", "orders")


def ensure_data_dirs() -> None:
    for path in (DATA_DIR, OUTPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def clear_output_dir(*, keep_names: frozenset[str] = frozenset({".gitkeep"})) -> list[str]:
    """Remove fund artifacts under ``OUTPUT_DIR``, keeping placeholder files.

    Returns the list of deleted path names (relative to ``OUTPUT_DIR``).
    """
    ensure_data_dirs()
    removed: list[str] = []
    for path in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.name):
        if path.name in keep_names:
            continue
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(path.name)
        elif path.is_dir():
            shutil.rmtree(path)
            removed.append(path.name + "/")
    return removed
