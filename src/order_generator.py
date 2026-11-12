"""Phase 1 synthetic multi-MIC order generator (public / offline).

Builds random PM orders priced near same-day VWAP (or close stand-in), then
runs the Phase 0 desk → broker → fill chain via ``tca_desk`` / ``tca_execution``.
Runnable without Phase 2 fund replay.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from market_tape import MarketTape, SyntheticMarketTape, load_market_tape
from order_schema import (
    DEFAULT_FUND_ID,
    SCHEMA_VERSION,
    make_id,
    validate,
    validate_links,
)
from tca_desk import generate_desk
from tca_execution import ImpactModel, simulate_fills


@dataclass
class GeneratorConfig:
    seed: int = 7
    n_orders: int = 40
    trade_date: str | None = None  # YYYY-MM-DD; default = today-like fixed demo day
    fund_id: str = DEFAULT_FUND_ID
    strategy: str = "VWAP"  # desk strategy for child routing
    share_min: int = 100
    share_max: int = 5_000
    buy_prob: float = 0.55
    reason: str = "REBALANCE"
    decision_time: str = "16:00"
    # Noise around VWAP for decision_price (fraction)
    price_noise: float = 0.002

    def as_dict(self) -> dict:
        return asdict(self)


def _pick_trade_date(cfg: GeneratorConfig, tape: MarketTape) -> pd.Timestamp:
    if cfg.trade_date:
        return pd.Timestamp(cfg.trade_date).normalize()
    # Prefer a date present in local prices if the tape loaded any
    prices = getattr(tape, "_prices", None)
    if prices is not None and len(prices):
        return pd.Timestamp(prices.date.max()).normalize()
    return pd.Timestamp("2024-06-21")


def generate_pm_orders(
    tape: MarketTape,
    cfg: GeneratorConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Sample random PM orders; return (pm, stock_days, venue_days)."""
    rng = np.random.default_rng(cfg.seed)
    day = _pick_trade_date(cfg, tape)
    decision = day - pd.Timedelta(days=1)
    # skip weekend-ish: keep simple calendar
    while decision.weekday() >= 5:
        decision -= pd.Timedelta(days=1)
    universe = tape.universe()
    if not universe:
        raise ValueError("tape universe empty")
    n = int(cfg.n_orders)
    permnos = [int(x) for x in rng.choice(universe, size=n, replace=True)]
    uniq = sorted(set(permnos))
    stock = tape.stock_days(pd.DatetimeIndex([day]), uniq)
    venue = tape.venue_days(pd.DatetimeIndex([day]), uniq)
    by = stock.set_index("permno")

    rows = []
    for i, permno in enumerate(permnos):
        m = by.loc[permno]
        if isinstance(m, pd.DataFrame):
            m = m.iloc[0]
        mid = float(m.day_vwap)
        noise = 1.0 + float(rng.normal(0.0, cfg.price_noise))
        px = max(mid * noise, 0.01)
        side = "BUY" if rng.random() < cfg.buy_prob else "SELL"
        shares = int(rng.integers(cfg.share_min, cfg.share_max + 1))
        reason = cfg.reason
        benchmark = "CLOSE" if reason in ("INITIAL", "REBALANCE") else "ARRIVAL"
        urgency = "HIGH" if reason == "OUTFLOW" else "MEDIUM"
        oid = make_id(cfg.seed, day.date(), permno, i, side)
        rows.append(
            {
                "order_id": oid,
                "fund_id": cfg.fund_id,
                "decision_date": decision,
                "decision_ts": decision + pd.Timedelta(cfg.decision_time + ":00"),
                "trade_date": day,
                "permno": int(permno),
                "side": side,
                "shares": shares,
                "decision_price": float(px),
                "reason": reason,
                "benchmark": benchmark,
                "urgency": urgency,
                "notional_usd": float(shares * px),
            }
        )
    pm = pd.DataFrame(rows)
    validate(pm, "pm_orders")
    used = set(pm.permno.unique())
    stock = stock[stock.permno.isin(used)].reset_index(drop=True)
    venue = venue[venue.permno.isin(used)].reset_index(drop=True)
    return pm, stock, venue


def generate_order_chain(
    cfg: GeneratorConfig | None = None,
    *,
    tape: MarketTape | None = None,
    model: ImpactModel | None = None,
    tape_mode: str = "synthetic",
) -> dict:
    """Full Phase 1 chain: PM → trader → broker → fills (+ stock/venue days)."""
    cfg = cfg or GeneratorConfig()
    tape = tape or load_market_tape(seed=cfg.seed, mode=tape_mode)
    pm, stock, venue = generate_pm_orders(tape, cfg)
    trader, broker = generate_desk(pm, stock, cfg.strategy, venue_days=venue)
    fills = simulate_fills(trader, broker, stock, model or ImpactModel())
    validate_links(pm, trader, broker, fills)
    return {
        "schema_version": SCHEMA_VERSION,
        "config": cfg.as_dict(),
        "tape": {
            "source": tape.meta.source,
            "mics": list(tape.meta.mics),
            "seed": tape.meta.seed,
            "mode": tape_mode,
        },
        "pm_orders": pm,
        "trader_orders": trader,
        "broker_orders": broker,
        "fills": fills,
        "stock_days": stock,
        "venue_days": venue,
    }


def default_tape(seed: int = 7) -> SyntheticMarketTape:
    return SyntheticMarketTape(seed=seed)
