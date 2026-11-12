#!/usr/bin/env python3
"""Phase 2: index-fund daybook + synthetic flows. No WRDS fetch.

Assumes ``fetch_data.py`` already wrote the four core CSVs and
``benchmark_sp500_tr.csv``. Flow::

    ensure_inputs → run_fund → print table previews

Default disk (core): ``daily_nav.csv``, ``fills.csv``, ``pm_orders.csv``.
Set ``DIVIDEND_SENSITIVITY=True`` to also write ``dividend_sensitivity.csv``.
Set ``PHASE2_WRITE_EXTRA=True`` in ``config.py`` for holdings, blotter, audit,
benchmark panels, notional summary, and ``run_manifest.json``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from paths import OUTPUT_2_FUND, ensure_data_dirs
from pipeline import PipelineConfig, ensure_inputs, run_fund
from preview import print_csv
from wrds_auth import load_project_dotenv

CORE_TABLES = (
    ("daily_nav.csv", "nav"),
    ("fills.csv", "fills"),
    ("pm_orders.csv", "pm_orders"),
)
EXTRA_TABLES = (
    ("daily_holdings.csv", "holdings"),
    ("orders.csv", "orders"),
    ("order_notional_summary.csv", "notional"),
    ("rebalance_schedule.csv", "schedule"),
    ("synthetic_flows.csv", "flows"),
    ("excluded_security_snapshots.csv", "exclusions"),
    ("padded_quotes.csv", "padded"),
    ("benchmark_daily.csv", "benchmark"),
    ("benchmark_by_year.csv", "benchmark_by_year"),
)


def main() -> int:
    load_project_dotenv()
    ensure_data_dirs()
    cfg = PipelineConfig.from_user_config(config)

    ensure_inputs(cfg)
    result = run_fund(cfg)
    print(
        f"ending_nav={result['ending_nav']:.4f} "
        f"sessions={result['sessions']} "
        f"fund_start={result['fund_start']} "
        f"fills=reference_close "
        f"write_extra={result.get('write_extra', False)} "
        f"dividend_sensitivity={result.get('dividend_sensitivity', False)}"
    )
    reasons = result.get("exclusion_reasons") or {}
    reason_txt = ",".join(f"{k}:{v}" for k, v in sorted(reasons.items())) or "none"
    print(
        f"excluded_rows={result.get('excluded_rows', 0)} "
        f"excluded_permnos={result.get('excluded_permnos', 0)} "
        f"flow_nonzero={result.get('flow_sessions_nonzero', 0)} "
        f"padded_quotes={result.get('padded_quote_rows', 0)} "
        f"reasons={reason_txt}"
    )
    print(
        f"pm_orders={result.get('pm_orders', 0)} "
        f"order_notional_usd={result.get('order_notional_usd', 0):.2f}"
    )
    td = result.get("tracking_difference_annualized_bps")
    te = result.get("tracking_error_annualized_bps")
    if td is not None and te is not None:
        print(f"tracking_difference_bps={td:.2f} tracking_error_bps={te:.2f}")
    sens = result.get("dividend_sensitivity_table")
    if sens is not None and len(sens):
        for row in sens.itertuples(index=False):
            print(
                f"div_mode={row.mode} ending_nav={row.ending_nav:.4f} "
                f"delta_nav={row.delta_ending_nav:.4f} "
                f"delta_td_bps={row.delta_td_bps:.2f} "
                f"delta_te_bps={row.delta_te_bps:.2f}"
            )
    for name, title in CORE_TABLES:
        print_csv(OUTPUT_2_FUND / name, title=title, blank_before=True)
    if result.get("dividend_sensitivity"):
        print_csv(
            OUTPUT_2_FUND / "dividend_sensitivity.csv",
            title="dividend_sensitivity",
            blank_before=True,
        )
    if result.get("write_extra"):
        for name, title in EXTRA_TABLES:
            print_csv(OUTPUT_2_FUND / name, title=title, blank_before=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
