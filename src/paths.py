"""Project path conventions matching dataset.md."""
from __future__ import annotations

import shutil
from pathlib import Path

# src/paths.py -> repo root is one level up
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
REVIEW_DIR = DATA_DIR / "review"
# Only the two human-edited decision CSVs live at REVIEW_DIR root; generated
# supporting files go under review/artifacts/.
REVIEW_ARTIFACTS_NAME = "artifacts"
OUTPUT_DIR = PROJECT_ROOT / "output" / "data"
CONFIG_DIR = PROJECT_ROOT / "configs"


def review_artifacts_dir() -> Path:
    return REVIEW_DIR / REVIEW_ARTIFACTS_NAME

BLOOMBERG_RAW = RAW_DIR / "bloomberg_corporate_actions.csv"
BLOOMBERG_RAW_LEGACY = DATA_DIR / "BMLL_Bloomberg_Corporate_Actions.csv"

CACHE_TABLES = (
    "weights",
    "wrds_corporate_actions",
    "wrds_delist_events",
    "wrds_adj_factors",
    "wrds_id_history",
    "bloomberg_corporate_actions",
    "unified_corporate_actions",
    "wrds_table_report",
)

EXTRA_CACHE_FILES = ("daily_prices.csv", "calendar.csv", "coverage.json", "manifest.json")


def ensure_data_dirs() -> None:
    for path in (RAW_DIR, CACHE_DIR, REVIEW_DIR, review_artifacts_dir(), OUTPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def clear_output_dir(*, keep_names: frozenset[str] = frozenset({".gitkeep"})) -> list[str]:
    """Remove ledger artifacts under ``OUTPUT_DIR``, keeping placeholder files.

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


def bloomberg_raw_path() -> Path:
    if BLOOMBERG_RAW.exists():
        return BLOOMBERG_RAW
    if BLOOMBERG_RAW_LEGACY.exists():
        return BLOOMBERG_RAW_LEGACY
    raise FileNotFoundError(
        f"Missing corporate-action CSV at {BLOOMBERG_RAW} "
        f"(or legacy path {BLOOMBERG_RAW_LEGACY})"
    )
