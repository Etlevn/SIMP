#!/usr/bin/env python3
"""Phase 3: one-date execution / TCA on Phase 2 PM orders (synthetic tape).

Reads ``output/2_fund/pm_orders.csv``, filters one trade date (default: latest
REBALANCE), builds synthetic stock_days from ``data/daily_prices.csv``, runs
CLOSE_AUCTION / VWAP / ARRIVAL_IS desk+impact fills, writes notional-weighted
summaries under ``output/3_tca/``.

Default disk: ``order_tca.csv``, ``strategy_summary.csv``.
Set ``PHASE3_WRITE_EXTRA=True`` for desk/fill panels and stock_days.
Impact coefficients are uncalibrated — mechanics only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

import config
from order_schema import SCHEMA_VERSION, validate
from paths import OUTPUT_2_FUND, OUTPUT_3_TCA, clear_output_dir, ensure_phase_output_dir
from preview import print_csv
from tca_execution import ImpactModel
from tca_market import build_synthetic_stock_days, ensure_data_dir_prices
from tca_report import run_strategies


def _load_pm() -> pd.DataFrame:
    path = OUTPUT_2_FUND / "pm_orders.csv"
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing {path}; run main_2_fund.py before Phase 3."
        )
    pm = pd.read_csv(path)
    pm["decision_date"] = pd.to_datetime(pm["decision_date"]).dt.normalize()
    pm["trade_date"] = pd.to_datetime(pm["trade_date"]).dt.normalize()
    pm["decision_ts"] = pd.to_datetime(pm["decision_ts"])
    validate(pm, "pm_orders")
    return pm


def _pick_trade_date(pm: pd.DataFrame, configured, reasons: tuple[str, ...]):
    sub = pm[pm.reason.isin(reasons)] if reasons else pm
    if sub.empty:
        raise ValueError(f"No pm_orders with reason in {reasons}")
    if configured:
        day = pd.Timestamp(configured).normalize()
        if not (sub.trade_date == day).any():
            raise ValueError(
                f"TCA_TRADE_DATE={day.date()} not in filtered pm_orders "
                f"(available: {sorted(sub.trade_date.dt.date.unique())[:8]}…)"
            )
        return day
    return pd.Timestamp(sub.trade_date.max()).normalize()


def main() -> int:
    write_extra = bool(getattr(config, "PHASE3_WRITE_EXTRA", False))
    reasons = tuple(getattr(config, "TCA_REASONS", ("REBALANCE",)))
    strategies = tuple(
        getattr(config, "TCA_STRATEGIES", ("CLOSE_AUCTION", "VWAP", "ARRIVAL_IS"))
    )
    model = ImpactModel(
        eta=float(getattr(config, "TCA_IMPACT_ETA", 0.6)),
        auction_eta=float(getattr(config, "TCA_IMPACT_AUCTION_ETA", 0.25)),
        cross_fraction=float(getattr(config, "TCA_IMPACT_CROSS", 0.5)),
    )

    pm_all = _load_pm()
    trade_date = _pick_trade_date(pm_all, getattr(config, "TCA_TRADE_DATE", None), reasons)
    pm = pm_all[
        pm_all.reason.isin(reasons) & (pm_all.trade_date == trade_date)
    ].reset_index(drop=True)
    if pm.empty:
        raise ValueError("Filtered pm_orders empty")

    prices = ensure_data_dir_prices()
    stock_days = build_synthetic_stock_days(pm, prices=prices)
    result = run_strategies(pm, stock_days, strategies=strategies, model=model)

    ensure_phase_output_dir(OUTPUT_3_TCA)
    clear_output_dir(OUTPUT_3_TCA)
    result["order_tca"].to_csv(OUTPUT_3_TCA / "order_tca.csv", index=False)
    result["strategy_summary"].to_csv(
        OUTPUT_3_TCA / "strategy_summary.csv", index=False
    )

    if write_extra:
        stock_days.to_csv(OUTPUT_3_TCA / "stock_days.csv", index=False)
        (OUTPUT_3_TCA / "impact_model.json").write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "tape_source": "synthetic_offline",
                    "trade_date": str(trade_date.date()),
                    "reasons": list(reasons),
                    "strategies": list(strategies),
                    "model": result["model"],
                    "disclaimer": "Impact coefficients uncalibrated; mechanics only.",
                },
                indent=2,
            )
            + "\n"
        )
        for strat in strategies:
            pack = result[strat]
            stem = strat.lower()
            pack["trader"].to_csv(OUTPUT_3_TCA / f"trader_{stem}.csv", index=False)
            pack["broker"].to_csv(OUTPUT_3_TCA / f"broker_{stem}.csv", index=False)
            pack["fills"].to_csv(OUTPUT_3_TCA / f"fills_{stem}.csv", index=False)

    summary = result["strategy_summary"]
    print(
        f"phase=3_tca schema_version={SCHEMA_VERSION} "
        f"trade_date={trade_date.date()} reasons={','.join(reasons)} "
        f"pm_orders={len(pm)} write_extra={write_extra} "
        f"tape=synthetic_offline impact=uncalibrated"
    )
    if len(summary):
        best = summary.sort_values("vs_close_bps").iloc[0]
        print(
            f"lowest_vs_close_bps={best.strategy}:{best.vs_close_bps:.2f} "
            f"(mechanics only — not for quoting)"
        )
    print_csv(OUTPUT_3_TCA / "strategy_summary.csv", title="summary", blank_before=True)
    print_csv(OUTPUT_3_TCA / "order_tca.csv", title="order_tca", blank_before=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
