"""Fill simulation with uncalibrated square-root impact (mechanics only).

Coefficients are literature defaults — not fitted to a vendor tape. Do not
quote as calibrated research costs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from order_schema import make_id, validate
from schema_example import DARK_MICS


@dataclass
class ImpactModel:
    eta: float = 0.6
    auction_eta: float = 0.25
    cross_fraction: float = 0.5
    default_auction_share: float = 0.10
    fee_take: float = 0.0030
    fee_make: float = -0.0020
    fee_auction: float = 0.0010
    fee_dark: float = 0.0010

    def as_dict(self) -> dict:
        return asdict(self)


def _window_share(start, end, n_bins: int = 13) -> float:
    """U-shaped continuous volume share between two timestamps."""
    day = pd.Timestamp(start).normalize()
    edges = pd.date_range(
        day + pd.Timedelta("09:30:00"),
        day + pd.Timedelta("16:00:00"),
        periods=n_bins + 1,
    )
    x = np.linspace(-1, 1, n_bins)
    curve = 0.15 + 0.85 * (x**2)
    curve = curve / curve.sum()
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    tot = 0.0
    for i in range(n_bins):
        lo, hi = max(edges[i], a), min(edges[i + 1], b)
        if hi > lo:
            tot += curve[i] * (hi - lo) / (edges[i + 1] - edges[i])
    return max(tot, 1e-6)


def simulate_fills(
    trader: pd.DataFrame,
    broker: pd.DataFrame,
    stock_days: pd.DataFrame,
    model: ImpactModel | None = None,
) -> pd.DataFrame:
    """Simulate child fills using synthetic stock_days."""
    model = model or ImpactModel()
    if broker is None or broker.empty:
        from order_schema import empty

        return empty("fills")

    if "permno" not in trader.columns:
        raise ValueError("trader_orders must include permno for fill simulation")

    s = stock_days.set_index(["date", "permno"])
    t = trader.set_index("trader_order_id")
    b = broker.merge(
        t.reset_index()[
            [
                "trader_order_id",
                "permno",
                "trade_date",
                "strategy",
                "start_ts",
                "end_ts",
                "shares",
            ]
        ].rename(columns={"shares": "parent_shares"}),
        on="trader_order_id",
        how="left",
    )
    b["date"] = pd.to_datetime(b["trade_date"]).dt.normalize()
    m = s.reindex(pd.MultiIndex.from_arrays([b["date"], b["permno"].astype(int)]))
    m.index = b.index

    sign = np.where(b.side.eq("BUY"), 1.0, -1.0)
    sigma = m.sigma_daily.to_numpy(float)
    auction_vol = m.close_auction_volume.fillna(
        model.default_auction_share * m.day_volume
    ).to_numpy(float)
    cont_vol = (m.day_volume.to_numpy(float) - auction_vol).clip(min=1.0)
    q_parent = b["parent_shares"].to_numpy(float)
    is_moc = b.order_type.eq("MOC").to_numpy()
    ws = {
        tid: _window_share(a, e) for tid, a, e in zip(t.index, t.start_ts, t.end_ts)
    }
    window = b.trader_order_id.map(ws).to_numpy(float)
    i_auc = model.auction_eta * sigma * np.sqrt(q_parent / np.maximum(auction_vol, 1.0))
    i_cont = model.eta * sigma * np.sqrt(q_parent / (cont_vol * window))
    dark = b.mic.isin(DARK_MICS).to_numpy()
    half_spread = m.spread_bps.fillna(5.0).to_numpy(float) / 2e4
    spread_cost = np.where(dark, 0.0, model.cross_fraction * half_spread)
    day_vwap = m.day_vwap.to_numpy(float)
    base = np.where(
        is_moc,
        m.close_price.to_numpy(float),
        np.where(b.strategy.eq("ARRIVAL_IS"), m.arrival_price.to_numpy(float), day_vwap),
    )
    impact = np.where(is_moc, i_auc, spread_cost + i_cont)
    price = base * (1.0 + sign * impact)
    qty = b.shares.to_numpy(int)
    fee_ps = np.where(
        is_moc,
        model.fee_auction,
        np.where(
            dark,
            model.fee_dark,
            model.cross_fraction * model.fee_take
            + (1 - model.cross_fraction) * model.fee_make,
        ),
    )
    liquidity = np.where(is_moc, "AUCTION", np.where(dark, "DARK", "REMOVE"))
    fills = pd.DataFrame(
        {
            "fill_id": [make_id(c, "F") for c in b.child_order_id],
            "child_order_id": b.child_order_id.values,
            "trader_order_id": b.trader_order_id.values,  # helper join key (not schema-required)
            "mic": b.mic.values,
            "fill_ts": np.where(
                is_moc,
                b.bucket_end,
                b.bucket_start + (b.bucket_end - b.bucket_start) / 2,
            ),
            "side": b.side.values,
            "shares": qty,
            "price": price,
            "liquidity": liquidity,
            "fee_usd": qty * fee_ps,
            "impact_bps": 1e4 * impact,
        }
    )
    fills["fill_ts"] = pd.to_datetime(fills.fill_ts)
    fills = fills[fills.shares > 0].reset_index(drop=True)
    # validate schema columns only (extra trader_order_id allowed)
    validate(fills.drop(columns=["trader_order_id"]), "fills")
    return fills
