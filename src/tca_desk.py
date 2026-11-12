"""PM → trader → broker desk layer for Phase 3 (personal Phase 0 column names).

Strategy assignment: main leg + spill when size exceeds a participation cap of
*expected* volume. Continuous legs split across MICs using same-day
``venue_days`` volumes when available; otherwise static ``VENUE_WEIGHTS``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from order_schema import make_id, validate, validate_links
from schema_example import DARK_MICS, OPEN, CLOSE, MOC_CUTOFF, VENUE_WEIGHTS, _alloc, _ts


def trader_orders_for_strategy(
    pm: pd.DataFrame,
    stock_days: pd.DataFrame,
    strategy: str,
    *,
    max_auction_participation: float = 0.25,
    max_participation: float = 0.10,
    is_participation: float = 0.15,
    expected_auction_share: float = 0.10,
    expected_event_auction_share: float = 0.20,
) -> pd.DataFrame:
    """One or two trader legs per PM order for the named desk strategy."""
    s = stock_days.set_index(["date", "permno"])
    rows = []
    for o in pm.itertuples(index=False):
        day = pd.Timestamp(o.trade_date).normalize()
        key = (day, int(o.permno))
        if key not in s.index:
            raise ValueError(f"No stock_days for {day.date()} PERMNO={o.permno}")
        m = s.loc[key]
        if isinstance(m, pd.DataFrame):
            m = m.iloc[0]
        is_event = bool(getattr(m, "is_index_event", False))
        auc_share = (
            expected_event_auction_share if is_event else expected_auction_share
        )
        exp_auction = float(m.adv_shares) * auc_share
        exp_cont = float(m.adv_shares) * (1 - auc_share)
        primary = str(getattr(m, "primary_mic", "XNYS") or "XNYS")
        q = int(o.shares)
        if strategy == "CLOSE_AUCTION":
            main = min(q, int(np.floor(max_auction_participation * exp_auction)))
            legs = [
                ("CLOSE_AUCTION", main, MOC_CUTOFF, CLOSE, 1.0, False),
                ("VWAP", q - main, OPEN, MOC_CUTOFF, max_participation, True),
            ]
        elif strategy == "VWAP":
            main = min(q, int(np.floor(max_participation * exp_cont)))
            legs = [
                ("VWAP", main, OPEN, MOC_CUTOFF, max_participation, False),
                ("CLOSE_AUCTION", q - main, MOC_CUTOFF, CLOSE, 1.0, True),
            ]
        elif strategy == "ARRIVAL_IS":
            main = min(q, int(np.floor(is_participation * exp_cont)))
            frac = min(1.0, max(30 / 390, main / max(is_participation * exp_cont, 1)))
            end = (_ts(day, OPEN) + pd.Timedelta(minutes=int(np.ceil(frac * 380)))).strftime(
                "%H:%M"
            )
            end = min(end, MOC_CUTOFF)
            legs = [
                ("ARRIVAL_IS", main, OPEN, end, is_participation, False),
                ("CLOSE_AUCTION", q - main, MOC_CUTOFF, CLOSE, 1.0, True),
            ]
        else:
            raise ValueError(f"Unknown strategy {strategy}")
        for strat, shares, start, end, cap, is_spill in legs:
            if shares <= 0:
                continue
            rows.append(
                {
                    "trader_order_id": make_id(o.order_id, strategy, strat, is_spill),
                    "order_id": o.order_id,
                    "permno": int(o.permno),
                    "strategy": strat,
                    "side": o.side,
                    "shares": int(shares),
                    "trade_date": day,
                    "start_ts": _ts(day, start),
                    "end_ts": _ts(day, end),
                    "max_participation": float(cap),
                    "limit_price": np.nan,
                    "primary_mic": primary,
                    "is_spill": bool(is_spill),
                }
            )
    out = pd.DataFrame(rows)
    validate(out, "trader_orders")
    return out


def _mic_weights_for(
    day: pd.Timestamp,
    permno: int,
    venue_days: pd.DataFrame | None,
) -> tuple[list[str], list[float]]:
    """Return (mics, weights) from venue_days volume, else static fallback."""
    if venue_days is not None and len(venue_days):
        v = venue_days[
            (pd.to_datetime(venue_days["date"]).dt.normalize() == day)
            & (venue_days["permno"].astype(int) == int(permno))
        ]
        if len(v) and float(v["volume"].sum()) > 0:
            mics = v["mic"].astype(str).tolist()
            weights = v["volume"].astype(float).tolist()
            return mics, weights
    return [m for m, _ in VENUE_WEIGHTS], [w for _, w in VENUE_WEIGHTS]


def broker_orders_from_trader(
    trader: pd.DataFrame,
    venue_days: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Split continuous legs across MICs; MOC stays on primary."""
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
        day = pd.Timestamp(t.trade_date).normalize()
        mics, weights = _mic_weights_for(day, int(t.permno), venue_days)
        mid = t.start_ts + (t.end_ts - t.start_ts) / 2
        buckets = [(t.start_ts, mid), (mid, t.end_ts)]
        bucket_shares = _alloc(int(t.shares), [0.55, 0.45])
        for bi, ((b0, b1), bq) in enumerate(zip(buckets, bucket_shares)):
            if bq <= 0:
                continue
            for mic, mq in zip(mics, _alloc(int(bq), weights)):
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


def generate_desk(
    pm: pd.DataFrame,
    stock_days: pd.DataFrame,
    strategy: str,
    venue_days: pd.DataFrame | None = None,
    **kw,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    trader = trader_orders_for_strategy(pm, stock_days, strategy, **kw)
    broker = broker_orders_from_trader(trader, venue_days=venue_days)
    validate_links(pm, trader, broker, fills=None)
    return trader, broker
