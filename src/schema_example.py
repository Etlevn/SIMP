"""Synthetic worked example for Phase 0: PM → ≥2 trader legs → MIC children → fills.

Uses fixed venue weights (not a live feed). Proves conservation and FK checks only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from order_schema import (
    DEFAULT_FUND_ID,
    SCHEMA_VERSION,
    conservation_report,
    make_id,
    validate,
    validate_links,
)

OPEN, MOC_CUTOFF, CLOSE = "09:30", "15:50", "16:00"
# Synthetic continuous-volume MIC mix (sums to 1)
VENUE_WEIGHTS = (("XNYS", 0.50), ("ARCX", 0.30), ("XADF", 0.20))
DARK_MICS = {"XADF", "TRF", "DARK"}


def _ts(day, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(day) + pd.Timedelta(hhmm + ":00")


def _alloc(total: int, weights) -> np.ndarray:
    w = np.asarray(weights, float)
    if total <= 0 or w.sum() <= 0:
        return np.zeros(len(w), int)
    raw = total * w / w.sum()
    base = np.floor(raw).astype(int)
    rem = int(total - base.sum())
    if rem:
        base[np.argsort(-(raw - base), kind="stable")[:rem]] += 1
    return base


def synthetic_pm_orders(*, fund_id: str = DEFAULT_FUND_ID) -> pd.DataFrame:
    """Two PM orders: large CLOSE rebalance + smaller ARRIVAL inflow."""
    day_d = pd.Timestamp("2024-06-20")
    day_t = pd.Timestamp("2024-06-21")
    rows = [
        {
            "order_id": make_id("pm", "rebalance", 10104),
            "fund_id": fund_id,
            "decision_date": day_d,
            "decision_ts": _ts(day_d, CLOSE),
            "trade_date": day_t,
            "permno": 10104,
            "ticker": "ORCL",
            "side": "BUY",
            "shares": 100_000,
            "decision_price": 125.0,
            "reason": "REBALANCE",
            "benchmark": "CLOSE",
            "urgency": "MEDIUM",
        },
        {
            "order_id": make_id("pm", "inflow", 10107),
            "fund_id": fund_id,
            "decision_date": day_d,
            "decision_ts": _ts(day_d, CLOSE),
            "trade_date": day_t,
            "permno": 10107,
            "ticker": "MSFT",
            "side": "BUY",
            "shares": 10_000,
            "decision_price": 420.0,
            "reason": "INFLOW",
            "benchmark": "ARRIVAL",
            "urgency": "MEDIUM",
        },
    ]
    out = pd.DataFrame(rows)
    out["notional_usd"] = out.shares * out.decision_price
    validate(out, "pm_orders")
    return out


def _trader_legs(pm: pd.DataFrame) -> pd.DataFrame:
    """CLOSE_AUCTION with VWAP spill for rebalance; ARRIVAL_IS + close spill for inflow."""
    rows = []
    for o in pm.itertuples(index=False):
        day = pd.Timestamp(o.trade_date)
        q = int(o.shares)
        if o.reason in ("INITIAL", "REBALANCE"):
            main = int(np.floor(0.70 * q))
            spill = q - main
            specs = [
                ("CLOSE_AUCTION", main, MOC_CUTOFF, CLOSE, 1.0, False),
                ("VWAP", spill, OPEN, MOC_CUTOFF, 0.10, True),
            ]
        else:
            main = int(np.floor(0.80 * q))
            spill = q - main
            specs = [
                ("ARRIVAL_IS", main, OPEN, "11:00", 0.15, False),
                ("CLOSE_AUCTION", spill, MOC_CUTOFF, CLOSE, 1.0, True),
            ]
        for strategy, shares, start, end, cap, is_spill in specs:
            if shares <= 0:
                continue
            rows.append(
                {
                    "trader_order_id": make_id(o.order_id, strategy, is_spill),
                    "order_id": o.order_id,
                    "strategy": strategy,
                    "side": o.side,
                    "shares": shares,
                    "trade_date": day,
                    "start_ts": _ts(day, start),
                    "end_ts": _ts(day, end),
                    "max_participation": cap,
                    "limit_price": np.nan,
                    "primary_mic": "XNYS",
                    "is_spill": bool(is_spill),
                }
            )
    out = pd.DataFrame(rows)
    validate(out, "trader_orders")
    return out


def _broker_children(trader: pd.DataFrame) -> pd.DataFrame:
    mics = [m for m, _ in VENUE_WEIGHTS]
    weights = [w for _, w in VENUE_WEIGHTS]
    rows = []
    for t in trader.itertuples(index=False):
        if t.strategy == "CLOSE_AUCTION":
            rows.append(
                {
                    "child_order_id": make_id(t.trader_order_id, t.primary_mic, "MOC"),
                    "trader_order_id": t.trader_order_id,
                    "mic": t.primary_mic,
                    "order_type": "MOC",
                    "tif": "AT_CLOSE",
                    "side": t.side,
                    "shares": int(t.shares),
                    "limit_price": np.nan,
                    "submit_ts": t.start_ts,
                    "bucket_start": t.start_ts,
                    "bucket_end": t.end_ts,
                }
            )
            continue
        # Continuous strategies: two synthetic half-day buckets × MIC mix
        mid = t.start_ts + (t.end_ts - t.start_ts) / 2
        buckets = [(t.start_ts, mid), (mid, t.end_ts)]
        bucket_shares = _alloc(int(t.shares), [0.55, 0.45])
        for bi, ((b0, b1), bq) in enumerate(zip(buckets, bucket_shares)):
            if bq <= 0:
                continue
            mic_shares = _alloc(int(bq), weights)
            for mic, mq in zip(mics, mic_shares):
                if mq <= 0:
                    continue
                dark = mic in DARK_MICS
                rows.append(
                    {
                        "child_order_id": make_id(t.trader_order_id, bi, mic),
                        "trader_order_id": t.trader_order_id,
                        "mic": mic,
                        "order_type": "MIDPOINT_PEG" if dark else "LIMIT",
                        "tif": "GTT",
                        "side": t.side,
                        "shares": int(mq),
                        "limit_price": np.nan,
                        "submit_ts": b0,
                        "bucket_start": b0,
                        "bucket_end": b1,
                    }
                )
    out = pd.DataFrame(rows)
    validate(out, "broker_orders")
    return out


def _fills_from_children(
    broker: pd.DataFrame, pm: pd.DataFrame, trader: pd.DataFrame
) -> pd.DataFrame:
    """One mechanical fill per child at the parent PM decision price (not TCA)."""
    px = (
        trader.merge(pm[["order_id", "decision_price"]], on="order_id", how="left")[
            ["trader_order_id", "decision_price"]
        ]
        .drop_duplicates("trader_order_id")
        .set_index("trader_order_id")["decision_price"]
    )
    rows = []
    for c in broker.itertuples(index=False):
        price = float(px.loc[c.trader_order_id])
        dark = c.mic in DARK_MICS
        liq = (
            "AUCTION"
            if c.order_type in ("MOC", "LOC")
            else ("DARK" if dark else "ADD")
        )
        rows.append(
            {
                "fill_id": make_id("fill", c.child_order_id),
                "child_order_id": c.child_order_id,
                "mic": c.mic,
                "fill_ts": c.bucket_end,
                "side": c.side,
                "shares": int(c.shares),
                "price": price,
                "liquidity": liq,
                "fee_usd": 0.0,
                "impact_bps": 0.0,
            }
        )
    out = pd.DataFrame(rows)
    validate(out, "fills")
    return out


def build_worked_example(*, fund_id: str = DEFAULT_FUND_ID) -> dict:
    """Return validated tables + conservation report for Phase 0 artifacts."""
    pm = synthetic_pm_orders(fund_id=fund_id)
    trader = _trader_legs(pm)
    broker = _broker_children(trader)
    fills = _fills_from_children(broker, pm, trader)
    validate_links(pm, trader, broker, fills)
    report = conservation_report(pm, trader, broker, fills)
    return {
        "schema_version": SCHEMA_VERSION,
        "pm_orders": pm,
        "trader_orders": trader,
        "broker_orders": broker,
        "fills": fills,
        "conservation_report": report,
    }
