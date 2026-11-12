"""Order-level TCA and notional-weighted strategy summaries (Phase 3).

Sign: positive bps = cost to the fund (buy above / sell below the reference).
For index rebalances the primary reference is the trade-date close.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from order_schema import validate, validate_links
from tca_desk import generate_desk
from tca_execution import ImpactModel, simulate_fills

STRATEGIES = ("CLOSE_AUCTION", "VWAP", "ARRIVAL_IS")
COST_COLS = [
    "vs_close_bps",
    "is_vs_decision_bps",
    "vs_arrival_bps",
    "vs_vwap_bps",
    "fees_bps",
]
# Fixed pct_adv bins (share of ADV): <1%, 1–5%, 5–10%, ≥10%
PCT_ADV_EDGES = (0.01, 0.05, 0.10)
PCT_ADV_LABELS = ("lt_1pct", "1_5pct", "5_10pct", "ge_10pct")


def pct_adv_bucket(pct_adv: pd.Series) -> pd.Series:
    """Map size/ADV into fixed liquidity labels."""
    x = pd.to_numeric(pct_adv, errors="coerce")
    bins = [-np.inf, *PCT_ADV_EDGES, np.inf]
    return pd.cut(x, bins=bins, labels=PCT_ADV_LABELS, right=False)


def order_tca(
    pm: pd.DataFrame,
    trader: pd.DataFrame,
    fills: pd.DataFrame,
    stock_days: pd.DataFrame,
    label: str,
) -> pd.DataFrame:
    """One row per PM order for a desk strategy label."""
    f = fills.merge(
        trader[["trader_order_id", "order_id", "strategy", "is_spill"]],
        on="trader_order_id",
    )
    f["value"] = f.shares * f.price
    g = f.groupby("order_id").agg(
        filled=("shares", "sum"),
        value=("value", "sum"),
        fees_usd=("fee_usd", "sum"),
    )
    spill = f[f.is_spill].groupby("order_id").shares.sum()
    auction = f[f.liquidity.eq("AUCTION")].groupby("order_id").shares.sum()
    x = pm.set_index("order_id").join(g).join(spill.rename("spill_shares")).join(
        auction.rename("auction_shares")
    )
    for col in ("filled", "value", "fees_usd", "spill_shares", "auction_shares"):
        if col not in x.columns:
            x[col] = 0.0
        x[col] = x[col].fillna(0.0)

    m = stock_days.set_index(["date", "permno"])
    trade_dates = pd.to_datetime(x.trade_date).dt.normalize()
    ref = m.reindex(pd.MultiIndex.from_arrays([trade_dates, x.permno.astype(int)]))
    ref.index = x.index
    x = x.join(
        ref[
            [
                "prev_close",
                "arrival_price",
                "close_price",
                "day_vwap",
                "sigma_daily",
                "adv_shares",
                "day_volume",
                "close_auction_volume",
            ]
        ]
    )
    s = np.where(x.side.eq("BUY"), 1.0, -1.0)
    x["avg_price"] = np.where(
        x.filled > 0, x.value / x.filled.replace(0, np.nan), np.nan
    )
    x["unfilled"] = x.shares - x.filled
    x["notional_usd"] = x.shares * x.decision_price

    def cost_bps(refcol: str) -> pd.Series:
        ex = s * x.filled * (x.avg_price.fillna(x[refcol]) - x[refcol])
        opp = s * x.unfilled * (x.close_price - x[refcol])
        return 1e4 * (ex + opp) / (x.shares * x[refcol])

    x["is_vs_decision_bps"] = cost_bps("decision_price")
    x["vs_arrival_bps"] = cost_bps("arrival_price")
    x["vs_close_bps"] = cost_bps("close_price")
    x["vs_vwap_bps"] = cost_bps("day_vwap")
    x["fees_bps"] = 1e4 * x.fees_usd / x.notional_usd.replace(0, np.nan)
    x["cost_vs_close_usd"] = (
        x.vs_close_bps / 1e4 * x.shares * x.close_price + x.fees_usd
    )
    x["pct_adv"] = x.shares / x.adv_shares.replace(0, np.nan)
    x["strategy"] = label
    x["tape_source"] = "synthetic_offline"
    return x.reset_index()


def strategy_summary(order_rows: pd.DataFrame) -> pd.DataFrame:
    """Notional-weighted means of cost columns per strategy."""
    if order_rows is None or order_rows.empty:
        return pd.DataFrame(
            columns=[
                "strategy",
                "orders",
                "n_trade_dates",
                "notional_usd",
                *COST_COLS,
                "cost_vs_close_usd",
            ]
        )

    def agg(g: pd.DataFrame) -> pd.Series:
        w = g["notional_usd"]
        out = {c: float(np.average(g[c], weights=w)) for c in COST_COLS}
        n_dates = (
            int(pd.to_datetime(g["trade_date"]).dt.normalize().nunique())
            if "trade_date" in g.columns
            else 0
        )
        out.update(
            orders=int(len(g)),
            n_trade_dates=n_dates,
            notional_usd=float(w.sum()),
            cost_vs_close_usd=float(g.cost_vs_close_usd.sum()),
            fees_usd=float(g.fees_usd.sum()),
        )
        return pd.Series(out)

    return (
        order_rows.groupby("strategy", sort=False)
        .apply(agg, include_groups=False)
        .reset_index()
    )


def liquidity_bucket_summary(order_rows: pd.DataFrame) -> pd.DataFrame:
    """Notional-weighted costs by strategy × pct_adv bucket."""
    cols = [
        "strategy",
        "pct_adv_bucket",
        "orders",
        "notional_usd",
        *COST_COLS,
        "cost_vs_close_usd",
    ]
    if order_rows is None or order_rows.empty:
        return pd.DataFrame(columns=cols)
    x = order_rows.copy()
    x["pct_adv_bucket"] = pct_adv_bucket(x["pct_adv"]).astype(str)

    def agg(g: pd.DataFrame) -> pd.Series:
        w = g["notional_usd"]
        out = {c: float(np.average(g[c], weights=w)) for c in COST_COLS}
        out.update(
            orders=int(len(g)),
            notional_usd=float(w.sum()),
            cost_vs_close_usd=float(g.cost_vs_close_usd.sum()),
        )
        return pd.Series(out)

    return (
        x.groupby(["strategy", "pct_adv_bucket"], sort=False)
        .apply(agg, include_groups=False)
        .reset_index()
    )


def run_strategies(
    pm: pd.DataFrame,
    stock_days: pd.DataFrame,
    strategies: tuple[str, ...] = STRATEGIES,
    model: ImpactModel | None = None,
    venue_days: pd.DataFrame | None = None,
    tape_source: str = "synthetic_offline",
    **desk_kw,
) -> dict:
    """Generate desk → fills → order TCA for each strategy on the same PM set."""
    model = model or ImpactModel()
    validate(pm, "pm_orders")
    out: dict = {}
    rows = []
    for strat in strategies:
        trader, broker = generate_desk(
            pm, stock_days, strat, venue_days=venue_days, **desk_kw
        )
        fills = simulate_fills(trader, broker, stock_days, model)
        validate_links(pm, trader, broker, fills)
        orders = order_tca(pm, trader, fills, stock_days, strat)
        orders["tape_source"] = tape_source
        out[strat] = {
            "trader": trader,
            "broker": broker,
            "fills": fills,
            "orders": orders,
        }
        rows.append(orders)
    all_orders = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    out["order_tca"] = all_orders
    out["strategy_summary"] = strategy_summary(all_orders)
    out["liquidity_buckets"] = liquidity_bucket_summary(all_orders)
    out["model"] = model.as_dict()
    return out
