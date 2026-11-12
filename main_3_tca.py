#!/usr/bin/env python3
"""Phase 3: execution / TCA on Phase 2 PM orders (MarketTape dual mode).

Reads ``output/2_fund/pm_orders.csv``, filters by ``TCA_REASONS`` and either one
trade date (``TCA_TRADE_DATE`` / latest) or all rebalance dates
(``TCA_ALL_REBALANCES=True``), loads tape via ``TAPE_MODE`` (synthetic default;
plugin requires ``SIMP_TAPE_PLUGIN_DIR``), runs CLOSE_AUCTION / VWAP /
ARRIVAL_IS desk+impact fills, writes summaries under ``output/3_tca/``.

Default disk: ``order_tca.csv``, ``strategy_summary.csv``, ``liquidity_buckets.csv``.
Set ``PHASE3_WRITE_EXTRA=True`` for desk/fill panels and stock/venue days.
Impact coefficients are uncalibrated — mechanics only.
``fund_td_bps`` / ``fund_te_bps`` on the strategy summary are Phase 2 fund-level
tracking metrics (not execution-cost-adjusted).
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
from benchmark import load_benchmark, performance_report
from market_tape import load_market_tape, stock_days_for_pm
from order_schema import SCHEMA_VERSION, validate
from paths import DATA_DIR, OUTPUT_2_FUND, OUTPUT_3_TCA, clear_output_dir, ensure_phase_output_dir
from preview import print_csv
from tca_execution import ImpactModel
from tca_market import ensure_data_dir_prices
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


def _fund_tracking_scalars() -> tuple[float | None, float | None]:
    """Phase 2 window TD/TE from daily_nav + local TR benchmark."""
    nav_path = OUTPUT_2_FUND / "daily_nav.csv"
    if not nav_path.is_file():
        return None, None
    nav = pd.read_csv(nav_path)
    nav["date"] = pd.to_datetime(nav["date"]).dt.normalize()
    try:
        bench = load_benchmark(DATA_DIR)
    except Exception:
        return None, None
    fund_start = nav["date"].iloc[0]
    fund_end = nav["date"].iloc[-1]
    try:
        _, summary, _ = performance_report(
            nav, bench, evaluation_start=fund_start, evaluation_end=fund_end
        )
    except Exception:
        return None, None
    return (
        float(summary["tracking_difference_annualized_bps"]),
        float(summary["tracking_error_annualized_bps"]),
    )


def main() -> int:
    write_extra = bool(getattr(config, "PHASE3_WRITE_EXTRA", False))
    reasons = tuple(getattr(config, "TCA_REASONS", ("REBALANCE",)))
    strategies = tuple(
        getattr(config, "TCA_STRATEGIES", ("CLOSE_AUCTION", "VWAP", "ARRIVAL_IS"))
    )
    all_rebalances = bool(getattr(config, "TCA_ALL_REBALANCES", False))
    tape_mode = str(getattr(config, "TAPE_MODE", "synthetic"))
    model = ImpactModel(
        eta=float(getattr(config, "TCA_IMPACT_ETA", 0.6)),
        auction_eta=float(getattr(config, "TCA_IMPACT_AUCTION_ETA", 0.25)),
        cross_fraction=float(getattr(config, "TCA_IMPACT_CROSS", 0.5)),
    )

    pm_all = _load_pm()
    filtered = pm_all[pm_all.reason.isin(reasons)] if reasons else pm_all
    if filtered.empty:
        raise ValueError(f"No pm_orders with reason in {reasons}")

    configured_day = getattr(config, "TCA_TRADE_DATE", None)
    if all_rebalances:
        if configured_day:
            print(
                f"note: TCA_ALL_REBALANCES=True ignores TCA_TRADE_DATE={configured_day}"
            )
        pm = filtered.reset_index(drop=True)
        trade_dates = sorted(pm.trade_date.dt.date.unique())
        date_label = f"all:{trade_dates[0]}..{trade_dates[-1]}(n={len(trade_dates)})"
        index_events = tuple(pd.Timestamp(d) for d in trade_dates)
    else:
        trade_date = _pick_trade_date(pm_all, configured_day, reasons)
        pm = filtered[filtered.trade_date == trade_date].reset_index(drop=True)
        if pm.empty:
            raise ValueError("Filtered pm_orders empty")
        trade_dates = [trade_date.date()]
        date_label = str(trade_date.date())
        index_events = (trade_date,)

    prices = ensure_data_dir_prices()
    tape = load_market_tape(
        seed=int(getattr(config, "GENERATOR_SEED", 7)),
        mode=tape_mode,
        prices=prices,
        index_event_dates=index_events,
    )
    stock_days, venue_days = stock_days_for_pm(pm, tape)
    result = run_strategies(
        pm,
        stock_days,
        strategies=strategies,
        model=model,
        venue_days=venue_days,
        tape_source=tape.meta.source,
    )

    fund_td, fund_te = _fund_tracking_scalars()
    summary = result["strategy_summary"].copy()
    summary["fund_td_bps"] = fund_td
    summary["fund_te_bps"] = fund_te
    buckets = result["liquidity_buckets"]

    ensure_phase_output_dir(OUTPUT_3_TCA)
    clear_output_dir(OUTPUT_3_TCA)
    result["order_tca"].to_csv(OUTPUT_3_TCA / "order_tca.csv", index=False)
    summary.to_csv(OUTPUT_3_TCA / "strategy_summary.csv", index=False)
    buckets.to_csv(OUTPUT_3_TCA / "liquidity_buckets.csv", index=False)

    if write_extra:
        stock_days.to_csv(OUTPUT_3_TCA / "stock_days.csv", index=False)
        venue_days.to_csv(OUTPUT_3_TCA / "venue_days.csv", index=False)
        (OUTPUT_3_TCA / "impact_model.json").write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "tape_source": tape.meta.source,
                    "tape_mode": tape_mode,
                    "trade_dates": [str(d) for d in trade_dates],
                    "all_rebalances": all_rebalances,
                    "reasons": list(reasons),
                    "strategies": list(strategies),
                    "fund_td_bps": fund_td,
                    "fund_te_bps": fund_te,
                    "model": result["model"],
                    "disclaimer": (
                        "Impact coefficients uncalibrated; mechanics only. "
                        "fund_td/te are Phase 2 fund-level (not cost-adjusted)."
                    ),
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

    print(
        f"phase=3_tca schema_version={SCHEMA_VERSION} "
        f"trade_dates={date_label} reasons={','.join(reasons)} "
        f"pm_orders={len(pm)} write_extra={write_extra} "
        f"tape={tape.meta.source} mode={tape_mode} impact=uncalibrated"
    )
    if fund_td is not None and fund_te is not None:
        print(
            f"fund_td_bps={fund_td:.2f} fund_te_bps={fund_te:.2f} "
            f"(Phase 2 fund-level — not execution-cost-adjusted)"
        )
    if len(summary):
        best = summary.sort_values("vs_close_bps").iloc[0]
        print(
            f"lowest_vs_close_bps={best.strategy}:{best.vs_close_bps:.2f} "
            f"(mechanics only — not for quoting)"
        )
    print_csv(OUTPUT_3_TCA / "strategy_summary.csv", title="summary", blank_before=True)
    print_csv(
        OUTPUT_3_TCA / "liquidity_buckets.csv",
        title="liquidity_buckets",
        blank_before=True,
    )
    print_csv(OUTPUT_3_TCA / "order_tca.csv", title="order_tca", blank_before=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
