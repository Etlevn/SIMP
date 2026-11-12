"""Project path conventions: flat ``data/`` inputs + phase ``output/<n>_*/``."""
from __future__ import annotations

import shutil
from pathlib import Path

# src/paths.py -> repo root is one level up
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_ROOT = PROJECT_ROOT / "output"
OUTPUT_0_SCHEMA = OUTPUT_ROOT / "0_schema"
OUTPUT_1_GENERATOR = OUTPUT_ROOT / "1_generator"
OUTPUT_2_FUND = OUTPUT_ROOT / "2_fund"
OUTPUT_3_TCA = OUTPUT_ROOT / "3_tca"
# Back-compat alias used by pipeline / fund writers
OUTPUT_DIR = OUTPUT_2_FUND
CONFIG_DIR = PROJECT_ROOT / "configs"

WEIGHTS_CSV = DATA_DIR / "weights.csv"
PRICES_CSV = DATA_DIR / "daily_prices.csv"
CALENDAR_CSV = DATA_DIR / "calendar.csv"
EVENTS_CSV = DATA_DIR / "events.csv"
COVERAGE_JSON = DATA_DIR / "coverage.json"
BENCHMARK_CSV = DATA_DIR / "benchmark_sp500_tr.csv"
BENCHMARK_META_JSON = DATA_DIR / "benchmark_sp500_tr.json"

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

# Phase 2 fund disk artifacts under output/2_fund/
# Core = TCA handoff; extras only when config.PHASE2_WRITE_EXTRA is True.
FUND_CORE_TABLES = ("daily_nav", "fills", "pm_orders")
FUND_EXTRA_SIM_TABLES = ("daily_holdings", "orders")  # from simulate_fund results
FUND_EXTRA_AUDIT_TABLES = (
    "rebalance_schedule",
    "synthetic_flows",
    "action_journal",
    "excluded_security_snapshots",
    "padded_quotes",
    "benchmark_daily",
    "benchmark_by_year",
    "order_notional_summary",
)
# Back-compat aliases
FUND_OUTPUT_TABLES = ("daily_nav", "daily_holdings", "fills", "orders")
FUND_AUDIT_TABLES = FUND_EXTRA_AUDIT_TABLES + ("pm_orders",)
RUN_MANIFEST_JSON = "run_manifest.json"

PHASE_OUTPUT_DIRS = (
    OUTPUT_0_SCHEMA,
    OUTPUT_1_GENERATOR,
    OUTPUT_2_FUND,
    OUTPUT_3_TCA,
)


def ensure_data_dirs() -> None:
    """Ensure ``data/`` and Phase 2 fund output dir exist."""
    for path in (DATA_DIR, OUTPUT_2_FUND):
        path.mkdir(parents=True, exist_ok=True)


def ensure_phase_output_dir(path: Path) -> Path:
    """Create one phase output directory and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_all_phase_output_dirs() -> None:
    for path in PHASE_OUTPUT_DIRS:
        path.mkdir(parents=True, exist_ok=True)


def clear_output_dir(
    directory: Path | None = None,
    *,
    keep_names: frozenset[str] = frozenset({".gitkeep"}),
) -> list[str]:
    """Remove artifacts under a phase output dir, keeping placeholders.

    Defaults to ``OUTPUT_2_FUND``. Returns deleted names relative to that dir.
    """
    out = directory if directory is not None else OUTPUT_2_FUND
    ensure_phase_output_dir(out)
    removed: list[str] = []
    for path in sorted(out.iterdir(), key=lambda p: p.name):
        if path.name in keep_names:
            continue
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(path.name)
        elif path.is_dir():
            shutil.rmtree(path)
            removed.append(path.name + "/")
    return removed
