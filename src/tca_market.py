"""Synthetic stock-day tape for Phase 3 TCA (mechanics only; not live venue data).

Builds one row per (trade_date, permno) from local CRSP closes. ADV / auction /
VWAP / spread / sigma are labeled stand-ins so desk + impact models can run
offline without a vendor-tape extract.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from paths import DATA_DIR, PRICES_CSV


def _load_prices(path=None) -> pd.DataFrame:
    p = pd.read_csv(path or PRICES_CSV)
    p["date"] = pd.to_datetime(p["date"]).dt.normalize()
    p["permno"] = p["permno"].astype(int)
    p["close_price"] = pd.to_numeric(p["close_price"], errors="coerce")
    return p.sort_values(["permno", "date"])


def build_synthetic_stock_days(
    pm: pd.DataFrame,
    *,
    prices: pd.DataFrame | None = None,
    default_spread_bps: float = 5.0,
    default_sigma: float = 0.012,
    adv_days: int = 20,
    auction_share: float = 0.10,
) -> pd.DataFrame:
    """Return synthetic ``stock_days`` covering every PM ``(trade_date, permno)``."""
    if pm is None or pm.empty:
        raise ValueError("pm_orders empty — nothing to build stock_days for")
    px = prices if prices is not None else _load_prices()
    keys = (
        pm[["trade_date", "permno"]]
        .assign(trade_date=lambda d: pd.to_datetime(d.trade_date).dt.normalize())
        .drop_duplicates()
    )
    # Previous close per permno via shift within price panel
    px = px.copy()
    px["prev_close"] = px.groupby("permno")["close_price"].shift(1)
    # Rolling ADV proxy in shares: notional_adv / close ≈ use dollar volume stand-in
    # ADV shares ≈ mean(close) * fictional share volume; use inverse-price scaled const
    px["synth_volume"] = 1e6  # flat synthetic day volume (shares)
    px["adv_shares"] = (
        px.groupby("permno")["synth_volume"]
        .transform(lambda s: s.rolling(adv_days, min_periods=1).mean())
    )

    m = keys.merge(
        px.rename(columns={"date": "trade_date"}),
        on=["trade_date", "permno"],
        how="left",
    )
    if m["close_price"].isna().any():
        n = int(m["close_price"].isna().sum())
        raise ValueError(f"{n} (trade_date, permno) missing from daily_prices.csv")

    # Fill prev_close with close when first day in panel
    m["prev_close"] = m["prev_close"].fillna(m["close_price"])
    m["arrival_price"] = m["prev_close"]  # open proxy
    m["day_vwap"] = 0.5 * (m["prev_close"] + m["close_price"])
    m["sigma_daily"] = float(default_sigma)
    m["spread_bps"] = float(default_spread_bps)
    m["day_volume"] = m["adv_shares"].astype(float)
    m["close_auction_volume"] = m["day_volume"] * float(auction_share)
    m["source"] = "synthetic_offline"
    out = m[
        [
            "trade_date",
            "permno",
            "prev_close",
            "arrival_price",
            "close_price",
            "day_vwap",
            "sigma_daily",
            "spread_bps",
            "adv_shares",
            "day_volume",
            "close_auction_volume",
            "source",
        ]
    ].rename(columns={"trade_date": "date"})
    return out.reset_index(drop=True)


def ensure_data_dir_prices() -> pd.DataFrame:
    if not PRICES_CSV.is_file():
        raise FileNotFoundError(f"Missing {PRICES_CSV}; run fetch_data.py first")
    return _load_prices()
