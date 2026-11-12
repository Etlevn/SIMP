"""Phase 3 market helpers — thin wrappers over ``market_tape``.

Kept so older imports continue to work; prefer ``stock_days_for_pm`` /
``load_market_tape`` directly.
"""
from __future__ import annotations

import pandas as pd

from market_tape import load_market_tape, stock_days_for_pm
from paths import PRICES_CSV


def ensure_data_dir_prices() -> pd.DataFrame:
    if not PRICES_CSV.is_file():
        raise FileNotFoundError(f"Missing {PRICES_CSV}; run fetch_data.py first")
    p = pd.read_csv(PRICES_CSV)
    p["date"] = pd.to_datetime(p["date"]).dt.normalize()
    p["permno"] = p["permno"].astype(int)
    p["close_price"] = pd.to_numeric(p["close_price"], errors="coerce")
    return p.sort_values(["permno", "date"])


def build_synthetic_stock_days(
    pm: pd.DataFrame,
    *,
    prices: pd.DataFrame | None = None,
    seed: int = 7,
    index_event_dates=(),
    **_ignored,
) -> pd.DataFrame:
    """Compatibility wrapper: stock_days only (venue_days discarded)."""
    tape = load_market_tape(
        seed=seed,
        mode="synthetic",
        prices=prices,
        index_event_dates=index_event_dates,
    )
    stock, _venue = stock_days_for_pm(pm, tape)
    return stock
